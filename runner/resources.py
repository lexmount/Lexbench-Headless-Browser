"""Portable resource telemetry for Agent Browser Bench.

The functional benchmark score and resource measurements intentionally remain
separate.  This module only observes:

* host state (low-frequency, enabled for normal runs);
* one browser engine process tree/cgroup during an opt-in attempt;
* fixture-server application traffic attributed by ``FixtureTrafficTracker``.

Linux cgroup v2 is used for cumulative CPU and kernel memory accounting when a
delegated parent is available.  PSS always comes from the complete engine
process tree via ``/proc/PID/smaps_rollup``.  The process-tree CPU fallback is
explicitly quality-flagged because a very short-lived child can exit between
samples.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import pathlib
import platform
import random
import re
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Any


HOST_TELEMETRY_SCHEMA = "abb_host_telemetry/1"
ENGINE_RESOURCE_SCHEMA = "abb_engine_resources/1"
RESOURCE_SUMMARY_SCHEMA = "abb_resource_summary/1"
FIXTURE_TRAFFIC_SCHEMA = "abb_fixture_traffic/1"
PROC_ROOT = pathlib.Path("/proc")
CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")
_CGROUP_SAFE = re.compile(r"[^0-9A-Za-z_.-]+")


def _read_text(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _read_int(path: pathlib.Path) -> int | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _now_iso() -> str:
    # UTC, matching runner.run.now_iso: artifacts carry no local timezone.
    return time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((percentile / 100.0) * len(ordered)))
    return float(ordered[min(rank - 1, len(ordered) - 1)])


def _median_ci(values: list[float], samples: int = 500) -> list[float] | None:
    """Deterministic bootstrap 95% CI for a median."""
    if not values:
        return None
    if len(values) == 1:
        value = float(values[0])
        return [value, value]
    rng = random.Random(0xABB04093 + len(values))
    medians = [
        float(statistics.median(rng.choice(values) for _ in values))
        for _ in range(samples)
    ]
    return [
        float(_percentile(medians, 2.5) or 0.0),
        float(_percentile(medians, 97.5) or 0.0),
    ]


def _metric_stats(values: list[float | int]) -> dict[str, Any]:
    numeric = [float(value) for value in values]
    if not numeric:
        return {"n": 0, "median": None, "p95": None, "median_ci95": None}
    return {
        "n": len(numeric),
        "median": float(statistics.median(numeric)),
        "p95": _percentile(numeric, 95),
        "median_ci95": _median_ci(numeric),
    }


@dataclass(frozen=True)
class ProcStat:
    pid: int
    ppid: int
    session: int
    state: str
    user_ticks: int
    system_ticks: int
    start_ticks: int

    @property
    def identity(self) -> tuple[int, int]:
        return (self.pid, self.start_ticks)


def parse_proc_stat(text: str) -> ProcStat:
    """Parse ``/proc/PID/stat`` without being confused by ')' in comm."""
    left = text.find("(")
    right = text.rfind(")")
    if left <= 0 or right <= left:
        raise ValueError("malformed proc stat")
    pid = int(text[:left].strip())
    tail = text[right + 1 :].strip().split()
    # tail starts at field 3 (state).
    if len(tail) < 20:
        raise ValueError("short proc stat")
    return ProcStat(
        pid=pid,
        ppid=int(tail[1]),
        session=int(tail[3]),
        state=tail[0],
        user_ticks=int(tail[11]),
        system_ticks=int(tail[12]),
        start_ticks=int(tail[19]),
    )


def read_proc_table(proc_root: pathlib.Path = PROC_ROOT) -> dict[int, ProcStat]:
    table: dict[int, ProcStat] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return table
    for entry in entries:
        if not entry.name.isdigit():
            continue
        text = _read_text(entry / "stat")
        if text is None:
            continue
        try:
            item = parse_proc_stat(text)
        except (ValueError, IndexError):
            continue
        table[item.pid] = item
    return table


def descendant_pids(root_pid: int, table: dict[int, ProcStat]) -> list[int]:
    children: dict[int, list[int]] = {}
    for item in table.values():
        children.setdefault(item.ppid, []).append(item.pid)
    found: list[int] = []
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid in table:
            found.append(pid)
            pending.extend(children.get(pid, ()))
    return sorted(found)


def read_process_tree(
    root_pid: int, proc_root: pathlib.Path = PROC_ROOT
) -> dict[int, ProcStat]:
    """Read descendants without scanning every process on the host.

    Linux exposes each task's direct children under
    ``/proc/PID/task/TID/children``. Walking all threads avoids missing a child
    forked by a non-leader thread and is substantially cheaper than parsing the
    global process table for every PSS sample.
    """

    table: dict[int, ProcStat] = {}
    pending = [int(root_pid)]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        text = _read_text(proc_root / str(pid) / "stat")
        if text is None:
            continue
        try:
            item = parse_proc_stat(text)
        except (ValueError, IndexError):
            continue
        table[pid] = item
        task_root = proc_root / str(pid) / "task"
        try:
            tids = list(task_root.iterdir())
        except OSError:
            tids = []
        for tid in tids:
            if not tid.name.isdigit():
                continue
            children = _read_text(tid / "children")
            for raw_child in (children or "").split():
                try:
                    child_pid = int(raw_child)
                except ValueError:
                    continue
                if child_pid not in seen:
                    pending.append(child_pid)
    return table


def read_pss_bytes(pid: int, proc_root: pathlib.Path = PROC_ROOT) -> int:
    text = (proc_root / str(pid) / "smaps_rollup").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("Pss:"):
            return int(line.split()[1]) * 1024
    raise ValueError("Pss missing from smaps_rollup")


def _process_tree_snapshot_once(
    root_pid: int,
    proc_root: pathlib.Path,
    candidate_pids: list[int] | None = None,
) -> dict[str, Any]:
    if candidate_pids is None:
        table = read_process_tree(root_pid, proc_root)
    else:
        table = {}
        for pid in sorted({int(root_pid), *(int(value) for value in candidate_pids)}):
            text = _read_text(proc_root / str(pid) / "stat")
            if text is None:
                continue
            try:
                table[pid] = parse_proc_stat(text)
            except (ValueError, IndexError):
                continue
    pids = sorted(table)
    counters: dict[tuple[int, int], tuple[int, int]] = {}
    pss_total = 0
    pss_errors: list[str] = []
    pss_zero_address_space_pids: list[int] = []

    def read_one_pss(pid: int) -> tuple[int | None, str | None, bool, int]:
        cpu_before = time.thread_time_ns()
        item = table[pid]
        try:
            return (
                read_pss_bytes(pid, proc_root),
                None,
                False,
                max(0, time.thread_time_ns() - cpu_before),
            )
        except (OSError, ValueError) as exc:
            # A zombie remains visible in /proc and in the process tree after
            # its address space has already been released.  Likewise, a PID
            # may disappear or be recycled between the tree and PSS reads.
            # Both cases have zero live PSS and are not missing measurements.
            current: ProcStat | None = None
            confirmed_gone = item.state in {"Z", "X", "x"}
            if not confirmed_gone:
                try:
                    current_text = (proc_root / str(pid) / "stat").read_text(
                        encoding="utf-8"
                    )
                    current = parse_proc_stat(current_text)
                except (FileNotFoundError, ProcessLookupError):
                    confirmed_gone = True
                except (OSError, ValueError, IndexError):
                    # Permission and malformed-read failures are genuine
                    # unavailable measurements, not zero-memory evidence.
                    current = None
                else:
                    confirmed_gone = (
                        current.identity != item.identity
                        or current.state in {"Z", "X", "x"}
                    )
            if confirmed_gone:
                return (
                    0,
                    None,
                    True,
                    max(0, time.thread_time_ns() - cpu_before),
                )
            return (
                None,
                f"{pid}:{type(exc).__name__}",
                False,
                max(0, time.thread_time_ns() - cpu_before),
            )

    for item in table.values():
        counters[item.identity] = (item.user_ticks, item.system_ticks)
    if len(pids) <= 1:
        pss_results = [read_one_pss(pid) for pid in pids]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(pids)),
            thread_name_prefix="abb-pss",
        ) as pool:
            pss_results = list(pool.map(read_one_pss, pids))
    pss_reader_cpu_ns = 0
    for pid, (value, error, zero_address_space, cpu_ns) in zip(pids, pss_results):
        pss_reader_cpu_ns += cpu_ns
        if value is not None:
            pss_total += value
        if error:
            pss_errors.append(error)
        if zero_address_space:
            pss_zero_address_space_pids.append(pid)
    return {
        "timestamp": _now_iso(),
        "monotonic_ns": time.monotonic_ns(),
        "root_pid": root_pid,
        "root_alive": root_pid in table,
        "pids": pids,
        "process_count": len(pids),
        "pss_bytes": pss_total if pids and not pss_errors else None,
        "pss_errors": pss_errors,
        "pss_zero_address_space_pids": pss_zero_address_space_pids,
        "cpu_user_ticks": sum(value[0] for value in counters.values()),
        "cpu_system_ticks": sum(value[1] for value in counters.values()),
        "_counters": counters,
        "_pss_reader_cpu_ns": pss_reader_cpu_ns,
    }


def process_tree_snapshot(
    root_pid: int,
    proc_root: pathlib.Path = PROC_ROOT,
    *,
    pss_scan_attempts: int = 3,
    candidate_pids: list[int] | None = None,
) -> dict[str, Any]:
    """Capture a process-tree snapshot, retrying transient PSS races.

    ``/proc`` cannot provide an atomic tree-plus-PSS snapshot.  A Chromium
    utility process can disappear between reading its ``stat`` and
    ``smaps_rollup`` files even though the engine root remains healthy.  A
    small, bounded full-tree retry avoids turning that expected race into a
    missing measurement while retaining the retry count as quality evidence.
    """

    attempts = max(1, int(pss_scan_attempts))
    snapshot: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        if candidate_pids is None:
            snapshot = _process_tree_snapshot_once(root_pid, proc_root)
        else:
            snapshot = _process_tree_snapshot_once(
                root_pid, proc_root, candidate_pids
            )
        snapshot["pss_scan_attempts"] = attempt
        if snapshot.get("pss_bytes") is not None or not snapshot.get("root_alive"):
            return snapshot
    assert snapshot is not None
    return snapshot


def _parse_key_values(text: str | None) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in (text or "").splitlines():
        pieces = line.split()
        if len(pieces) < 2:
            continue
        try:
            values[pieces[0]] = int(pieces[1])
        except ValueError:
            continue
    return values


def _current_cgroup() -> pathlib.Path | None:
    text = _read_text(PROC_ROOT / "self" / "cgroup")
    for line in (text or "").splitlines():
        if line.startswith("0::"):
            rel = line[3:].lstrip("/")
            return CGROUP_ROOT / rel
    return None


def _cgroup_parent_candidates() -> list[pathlib.Path]:
    current = _current_cgroup()
    if current is None:
        return []
    candidates: list[pathlib.Path] = []
    candidate = current
    while candidate == CGROUP_ROOT or CGROUP_ROOT in candidate.parents:
        candidates.append(candidate)
        if candidate == CGROUP_ROOT:
            break
        candidate = candidate.parent
    return candidates


class CgroupV2Group:
    """One engine cgroup under an already-delegated cgroup-v2 parent."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    @classmethod
    def create(cls, label: str) -> tuple["CgroupV2Group | None", str | None]:
        raw = _CGROUP_SAFE.sub("-", label).strip("-._") or "engine"
        digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]
        name = f"abb-{os.getpid()}-{raw[:36]}-{digest}.scope"
        errors: list[str] = []
        for parent in _cgroup_parent_candidates():
            subtree = set((_read_text(parent / "cgroup.subtree_control") or "").split())
            if "cpu" not in subtree:
                continue
            path = parent / name
            try:
                path.mkdir()
                if not (path / "cpu.stat").exists():
                    path.rmdir()
                    continue
                return cls(path), None
            except OSError as exc:
                errors.append(f"{parent}:{exc.errno}")
        reason = "no delegated cgroup-v2 parent with cpu controller"
        if errors:
            reason += " (" + ", ".join(errors[:3]) + ")"
        return None, reason

    def add_process_tree(self, root_pid: int) -> list[str]:
        errors: list[str] = []
        table = read_proc_table()
        pids = descendant_pids(root_pid, table) or [root_pid]
        for pid in pids:
            try:
                (self.path / "cgroup.procs").write_text(str(pid), encoding="ascii")
            except OSError as exc:
                errors.append(f"{pid}:{exc.errno}")
        return errors

    def cpu_stat(self) -> dict[str, int] | None:
        values = _parse_key_values(_read_text(self.path / "cpu.stat"))
        return values or None

    def process_ids(self) -> list[int] | None:
        text = _read_text(self.path / "cgroup.procs")
        if text is None:
            return None
        values: list[int] = []
        for raw in text.split():
            try:
                values.append(int(raw))
            except ValueError:
                continue
        return sorted(set(values))

    def memory_current(self) -> int | None:
        return _read_int(self.path / "memory.current")

    def memory_peak(self) -> int | None:
        return _read_int(self.path / "memory.peak")

    def reset_memory_peak(self) -> str | None:
        path = self.path / "memory.peak"
        if not path.exists():
            return "memory controller unavailable"
        try:
            path.write_text("0", encoding="ascii")
            return None
        except OSError as exc:
            return f"memory.peak reset failed: errno={exc.errno}"

    def cleanup(self) -> str | None:
        kill_path = self.path / "cgroup.kill"
        if kill_path.exists():
            try:
                kill_path.write_text("1", encoding="ascii")
            except OSError:
                pass
        for _ in range(20):
            try:
                self.path.rmdir()
                return None
            except FileNotFoundError:
                return None
            except OSError:
                time.sleep(0.01)
        try:
            self.path.rmdir()
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"cgroup cleanup failed: errno={exc.errno}"


def _public_process_sample(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in sample.items()
        if not str(key).startswith("_")
    }


class EngineProcessSampler:
    """Sample one complete engine process tree during an attempt."""

    def __init__(
        self,
        root_pid: int,
        cgroup: CgroupV2Group | None,
        sample_interval_ms: int,
        *,
        reset_memory_peak: bool = True,
        cgroup_cpu_baseline: dict[str, int] | None = None,
    ) -> None:
        self.root_pid = int(root_pid)
        self.cgroup = cgroup
        self.sample_interval_ms = max(10, int(sample_interval_ms))
        self.reset_memory_peak_on_start = reset_memory_peak
        self.cgroup_cpu_baseline = cgroup_cpu_baseline
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sampler_cpu_ns = 0
        self._started_ns = 0
        self._peak_reset_error: str | None = None

    def _capture(self, phase: str) -> None:
        cpu_before = time.thread_time_ns()
        cgroup_pids = self.cgroup.process_ids() if self.cgroup is not None else None
        sample = process_tree_snapshot(
            self.root_pid,
            candidate_pids=cgroup_pids,
        )
        sample["phase"] = phase
        sample["elapsed_ms"] = (time.monotonic_ns() - self._started_ns) / 1_000_000
        if self.cgroup is not None:
            sample["cgroup_cpu"] = self.cgroup.cpu_stat()
            sample["cgroup_memory_current_bytes"] = self.cgroup.memory_current()
            sample["cgroup_memory_peak_bytes"] = self.cgroup.memory_peak()
        else:
            sample["cgroup_cpu"] = None
            sample["cgroup_memory_current_bytes"] = None
            sample["cgroup_memory_peak_bytes"] = None
        self.samples.append(sample)
        self._sampler_cpu_ns += max(0, time.thread_time_ns() - cpu_before)
        self._sampler_cpu_ns += int(sample.get("_pss_reader_cpu_ns") or 0)

    def start(self) -> None:
        self._started_ns = time.monotonic_ns()
        if self.cgroup is not None and self.reset_memory_peak_on_start:
            self._peak_reset_error = self.cgroup.reset_memory_peak()
        self._capture("baseline")

        def loop() -> None:
            while not self._stop.wait(self.sample_interval_ms / 1000.0):
                self._capture("sample")

        self._thread = threading.Thread(
            target=loop,
            name=f"abb-resource-{self.root_pid}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, duration_ms: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.sample_interval_ms / 1000.0 * 2))
        self._capture("end")
        summary = self._summarize(max(0, int(duration_ms)))
        return summary, [_public_process_sample(sample) for sample in self.samples]

    def _summarize(self, duration_ms: int) -> dict[str, Any]:
        first = self.samples[0]
        last = self.samples[-1]
        unavailable: dict[str, str] = {}
        quality_flags: list[str] = []

        pss_values = [
            int(sample["pss_bytes"])
            for sample in self.samples
            if sample.get("pss_bytes") is not None
        ]
        if len(pss_values) != len(self.samples):
            unavailable["pss"] = "one or more complete process-tree PSS scans failed"
            quality_flags.append("pss_scan_incomplete")
        if any(int(sample.get("pss_scan_attempts") or 1) > 1 for sample in self.samples):
            quality_flags.append("pss_scan_retried")
        if any(sample.get("pss_zero_address_space_pids") for sample in self.samples):
            quality_flags.append("pss_zero_address_space_process")
        pss_baseline = first.get("pss_bytes")
        pss_end = last.get("pss_bytes")
        pss_peak = max(pss_values) if pss_values and "pss" not in unavailable else None

        cpu_total_ms: float | None = None
        cpu_user_ms: float | None = None
        cpu_system_ms: float | None = None
        cpu_backend = "proc_tree"
        cgroup_start = self.cgroup_cpu_baseline or first.get("cgroup_cpu")
        cgroup_end = last.get("cgroup_cpu")
        if cgroup_start and cgroup_end:
            cpu_backend = "cgroup_v2"
            cpu_total_ms = max(
                0.0,
                (int(cgroup_end.get("usage_usec", 0)) - int(cgroup_start.get("usage_usec", 0))) / 1000.0,
            )
            cpu_user_ms = max(
                0.0,
                (int(cgroup_end.get("user_usec", 0)) - int(cgroup_start.get("user_usec", 0))) / 1000.0,
            )
            cpu_system_ms = max(
                0.0,
                (int(cgroup_end.get("system_usec", 0)) - int(cgroup_start.get("system_usec", 0))) / 1000.0,
            )
        else:
            ticks = float(os.sysconf("SC_CLK_TCK"))
            start_counters = first.get("_counters") or {}
            maxima: dict[tuple[int, int], tuple[int, int]] = {}
            for sample in self.samples:
                for identity, value in (sample.get("_counters") or {}).items():
                    previous = maxima.get(identity, (0, 0))
                    maxima[identity] = (max(previous[0], value[0]), max(previous[1], value[1]))
            user_ticks = 0
            system_ticks = 0
            for identity, maximum in maxima.items():
                initial = start_counters.get(identity, (0, 0))
                user_ticks += max(0, maximum[0] - initial[0])
                system_ticks += max(0, maximum[1] - initial[1])
            cpu_user_ms = user_ticks * 1000.0 / ticks
            cpu_system_ms = system_ticks * 1000.0 / ticks
            cpu_total_ms = cpu_user_ms + cpu_system_ms
            quality_flags.append("proc_tree_child_exit_loss_risk")

        if not last.get("root_alive"):
            quality_flags.append("engine_root_exited")
        if len(self.samples) <= 2:
            quality_flags.append("baseline_end_only")
        if self._peak_reset_error:
            quality_flags.append("cgroup_memory_peak_not_reset")

        cgroup_memory_values = [
            int(sample["cgroup_memory_current_bytes"])
            for sample in self.samples
            if sample.get("cgroup_memory_current_bytes") is not None
        ]
        cgroup_memory_peak_values = [
            int(sample["cgroup_memory_peak_bytes"])
            for sample in self.samples
            if sample.get("cgroup_memory_peak_bytes") is not None
        ]
        cgroup_memory_baseline = first.get("cgroup_memory_current_bytes")
        cgroup_memory_end = last.get("cgroup_memory_current_bytes")
        cgroup_memory_peak = max(cgroup_memory_peak_values) if cgroup_memory_peak_values else None
        if self.cgroup is None:
            unavailable["cgroup_memory"] = "cgroup v2 backend unavailable"
        elif not cgroup_memory_values:
            unavailable["cgroup_memory"] = "cgroup memory controller unavailable"

        return {
            "schema": ENGINE_RESOURCE_SCHEMA,
            "scope": "engine_scope",
            "measurement_backend": {
                "cpu": cpu_backend,
                "memory_pss": "proc_smaps_rollup",
                "memory_accounting": "cgroup_v2" if cgroup_memory_values else "unavailable",
            },
            "cpu_total_ms": round(cpu_total_ms, 3) if cpu_total_ms is not None else None,
            "cpu_user_ms": round(cpu_user_ms, 3) if cpu_user_ms is not None else None,
            "cpu_system_ms": round(cpu_system_ms, 3) if cpu_system_ms is not None else None,
            "avg_cores": round(cpu_total_ms / duration_ms, 6)
            if cpu_total_ms is not None and duration_ms > 0
            else None,
            "pss_baseline_bytes": pss_baseline,
            "pss_peak_bytes": pss_peak,
            "pss_end_bytes": pss_end,
            "pss_peak_delta_bytes": max(0, int(pss_peak) - int(pss_baseline))
            if pss_peak is not None and pss_baseline is not None
            else None,
            "process_count_baseline": first.get("process_count"),
            "process_count_peak": max(int(sample.get("process_count") or 0) for sample in self.samples),
            "process_count_end": last.get("process_count"),
            "cgroup_memory_baseline_bytes": cgroup_memory_baseline,
            "cgroup_memory_peak_bytes": cgroup_memory_peak,
            "cgroup_memory_end_bytes": cgroup_memory_end,
            "cgroup_memory_peak_delta_bytes": max(0, int(cgroup_memory_peak) - int(cgroup_memory_baseline))
            if cgroup_memory_peak is not None and cgroup_memory_baseline is not None
            else None,
            "sample_interval_ms": self.sample_interval_ms,
            "samples_seen": len(self.samples),
            "sampler_cpu_ms": round(self._sampler_cpu_ns / 1_000_000, 3),
            "quality_flags": sorted(set(quality_flags)),
            "unavailable": unavailable,
        }


def write_resource_samples(
    path: pathlib.Path,
    process_samples: list[dict[str, Any]],
    traffic_events: list[dict[str, Any]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for sample in process_samples:
            handle.write(json.dumps({"kind": "engine_sample", **sample}, sort_keys=True) + "\n")
        for event in traffic_events or []:
            handle.write(json.dumps({"kind": "fixture_traffic", **event}, sort_keys=True) + "\n")


def _request_header_bytes(command: str, path: str, version: str, headers: dict[str, str]) -> int:
    total = len(f"{command} {path} {version}\r\n".encode("latin-1", "replace"))
    total += sum(
        len(str(name).encode("latin-1", "replace"))
        + 2
        + len(str(value).encode("latin-1", "replace"))
        + 2
        for name, value in headers.items()
    )
    return total + 2


def _response_header_bytes(
    status: int,
    headers: list[tuple[str, str]],
    *,
    version: str,
    reason: str,
) -> int:
    total = len(f"{version} {status} {reason}\r\n".encode("latin-1", "replace"))
    total += sum(
        len(str(name).encode("latin-1", "replace"))
        + 2
        + len(str(value).encode("latin-1", "replace"))
        + 2
        for name, value in headers
    )
    return total + 2


class FixtureTrafficTracker:
    """Attribute fixture traffic to active attempts without CDP instrumentation.

    A jobs=1 resource run is mechanically unambiguous.  With concurrent
    attempts, session values in URL/referrer/body are used when present;
    otherwise the request is retained as ambiguous and the affected attempts
    receive a quality flag rather than fabricated zero-byte measurements.
    """

    EXCLUDED_PREFIXES = ("/__grade__/", "/__event__/")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._contexts: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, str] = {}

    def begin_attempt(self, token: str, session: str) -> None:
        with self._lock:
            self._active.add(token)
            self._sessions[session] = token
            self._contexts[token] = {
                "token": token,
                "session": session,
                "started_monotonic_ns": time.monotonic_ns(),
                "fixture": self._empty_counts(),
                "harness": self._empty_counts(),
                "events": [],
                "ambiguous_requests": 0,
                "inflight": 0,
            }

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {
            "requests": 0,
            "rx_body_bytes": 0,
            "tx_body_bytes": 0,
            "rx_header_bytes": 0,
            "tx_header_bytes": 0,
        }

    def _resolve_token(self, haystack: str) -> tuple[str | None, bool]:
        matches = {
            token
            for session, token in self._sessions.items()
            if token in self._active and session and session in haystack
        }
        if len(matches) == 1:
            return next(iter(matches)), False
        if len(self._active) == 1:
            return next(iter(self._active)), False
        return None, bool(self._active)

    def begin_request(
        self,
        *,
        path: str,
        command: str,
        request_version: str,
        headers: dict[str, str],
        request_body: bytes,
    ) -> dict[str, Any]:
        body_text = request_body[:8192].decode("utf-8", "ignore")
        haystack = "\n".join(
            [path, headers.get("Referer", ""), headers.get("Cookie", ""), body_text]
        )
        with self._lock:
            token, ambiguous = self._resolve_token(haystack)
            if ambiguous:
                for active in self._active:
                    context = self._contexts.get(active)
                    if context is not None:
                        context["ambiguous_requests"] += 1
            scope = "harness_scope" if path.startswith(self.EXCLUDED_PREFIXES) else "fixture_app"
            if token is not None and token in self._contexts:
                self._contexts[token]["inflight"] += 1
            return {
                "token": token,
                "scope": scope,
                "path": path,
                "command": command,
                "request_body_bytes": len(request_body),
                "request_header_bytes": _request_header_bytes(
                    command, path, request_version, headers
                ),
                "started_monotonic_ns": time.monotonic_ns(),
            }

    def finish_request(
        self,
        request: dict[str, Any] | None,
        *,
        status: int,
        response_headers: list[tuple[str, str]],
        response_body_bytes: int,
        request_stream_body_bytes: int = 0,
        response_version: str = "HTTP/1.0",
        response_reason: str = "",
    ) -> None:
        if not request:
            return
        token = request.get("token")
        if token is None:
            return
        with self._lock:
            context = self._contexts.get(str(token))
            if context is None:
                return
            target = context["harness"] if request["scope"] == "harness_scope" else context["fixture"]
            target["requests"] += 1
            target["rx_body_bytes"] += int(request["request_body_bytes"]) + int(request_stream_body_bytes)
            target["tx_body_bytes"] += int(response_body_bytes)
            target["rx_header_bytes"] += int(request["request_header_bytes"])
            target["tx_header_bytes"] += _response_header_bytes(
                status,
                response_headers,
                version=response_version,
                reason=response_reason,
            )
            context["inflight"] = max(0, int(context["inflight"]) - 1)
            context["events"].append(
                {
                    "timestamp": _now_iso(),
                    "scope": request["scope"],
                    "method": request["command"],
                    "path": request["path"],
                    "status": status,
                    "rx_body_bytes": int(request["request_body_bytes"]) + int(request_stream_body_bytes),
                    "tx_body_bytes": int(response_body_bytes),
                    "duration_ms": round(
                        (time.monotonic_ns() - int(request["started_monotonic_ns"])) / 1_000_000,
                        3,
                    ),
                }
            )

    def end_attempt(self, token: str, grace_s: float = 0.2) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, grace_s)
        while time.monotonic() < deadline:
            with self._lock:
                context = self._contexts.get(token)
                if context is None or int(context["inflight"]) == 0:
                    break
            time.sleep(0.01)
        with self._lock:
            self._active.discard(token)
            context = self._contexts.get(token)
            if context is None:
                return {
                    "schema": FIXTURE_TRAFFIC_SCHEMA,
                    "available": False,
                    "reason": "attempt traffic context missing",
                    "events": [],
                }
            session = str(context["session"])
            self._sessions.pop(session, None)
            fixture_counts = dict(context["fixture"])
            harness_counts = dict(context["harness"])
            ambiguous = int(context["ambiguous_requests"])
            inflight = int(context["inflight"])
            events = list(context["events"])
            del self._contexts[token]
        available = ambiguous == 0 and inflight == 0
        reason = None
        if ambiguous:
            reason = f"{ambiguous} concurrent request(s) could not be bound to an attempt"
        elif inflight:
            reason = f"{inflight} request(s) still in flight at attempt end"
        return {
            "schema": FIXTURE_TRAFFIC_SCHEMA,
            "available": available,
            "reason": reason,
            "byte_semantics": "HTTP application headers and bodies; WebSocket frame payload bodies; excludes grader/event endpoints",
            "fixture_app_rx_body_bytes": fixture_counts["rx_body_bytes"],
            "fixture_app_tx_body_bytes": fixture_counts["tx_body_bytes"],
            "fixture_app_rx_header_bytes": fixture_counts["rx_header_bytes"],
            "fixture_app_tx_header_bytes": fixture_counts["tx_header_bytes"],
            "fixture_request_count": fixture_counts["requests"],
            "excluded_harness_rx_body_bytes": harness_counts["rx_body_bytes"],
            "excluded_harness_tx_body_bytes": harness_counts["tx_body_bytes"],
            "excluded_harness_request_count": harness_counts["requests"],
            "ambiguous_request_count": ambiguous,
            "inflight_request_count": inflight,
            "events": events,
        }


def _parse_meminfo() -> dict[str, int] | None:
    text = _read_text(PROC_ROOT / "meminfo")
    if text is None:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, raw = line.partition(":")
        pieces = raw.strip().split()
        if not pieces:
            continue
        try:
            value = int(pieces[0])
        except ValueError:
            continue
        if len(pieces) > 1 and pieces[1].lower() == "kb":
            value *= 1024
        values[name] = value
    return values


def _parse_pressure(name: str) -> dict[str, dict[str, float | int]] | None:
    text = _read_text(PROC_ROOT / "pressure" / name)
    if text is None:
        return None
    result: dict[str, dict[str, float | int]] = {}
    for line in text.splitlines():
        pieces = line.split()
        if not pieces:
            continue
        row: dict[str, float | int] = {}
        for piece in pieces[1:]:
            key, _, raw = piece.partition("=")
            try:
                row[key] = int(raw) if key == "total" else float(raw)
            except ValueError:
                continue
        result[pieces[0]] = row
    return result


def _vmstat_swap() -> dict[str, int] | None:
    values = _parse_key_values(_read_text(PROC_ROOT / "vmstat"))
    if not values:
        return None
    return {
        "pswpin_pages": int(values.get("pswpin", 0)),
        "pswpout_pages": int(values.get("pswpout", 0)),
    }


def _descendant_count(root_pid: int) -> int | None:
    table = read_proc_table()
    if root_pid not in table:
        return None
    return len(descendant_pids(root_pid, table)) - 1


def host_snapshot(root_pid: int) -> dict[str, Any]:
    mem = _parse_meminfo()
    load = None
    try:
        one, five, fifteen = os.getloadavg()
        load = {"one": one, "five": five, "fifteen": fifteen}
    except OSError:
        pass
    return {
        "schema": HOST_TELEMETRY_SCHEMA,
        "timestamp": _now_iso(),
        "monotonic_ns": time.monotonic_ns(),
        "loadavg": load,
        "memory": None
        if mem is None
        else {
            "total_bytes": mem.get("MemTotal"),
            "available_bytes": mem.get("MemAvailable"),
            "swap_total_bytes": mem.get("SwapTotal"),
            "swap_free_bytes": mem.get("SwapFree"),
            "swap_used_bytes": max(
                0, int(mem.get("SwapTotal", 0)) - int(mem.get("SwapFree", 0))
            ),
        },
        "psi": {
            "cpu": _parse_pressure("cpu"),
            "memory": _parse_pressure("memory"),
            "io": _parse_pressure("io"),
        },
        "vmstat": _vmstat_swap(),
        "bench_descendant_process_count": _descendant_count(root_pid),
    }


def evaluate_host_pollution(samples: list[dict[str, Any]]) -> dict[str, Any]:
    flags: list[str] = []
    if not samples:
        return {
            "polluted": True,
            "flags": ["host_telemetry_missing"],
            "sample_count": 0,
        }
    memories = [sample.get("memory") for sample in samples if sample.get("memory")]
    if memories:
        if max(int(item.get("swap_used_bytes") or 0) for item in memories) > 0:
            flags.append("swap_in_use")
        ratios = [
            float(item.get("available_bytes") or 0) / float(item.get("total_bytes") or 1)
            for item in memories
        ]
        if min(ratios) < 0.05:
            flags.append("low_memory_available")
    vmstats = [sample.get("vmstat") for sample in samples if sample.get("vmstat")]
    if len(vmstats) >= 2:
        if (
            int(vmstats[-1].get("pswpin_pages", 0)) > int(vmstats[0].get("pswpin_pages", 0))
            or int(vmstats[-1].get("pswpout_pages", 0))
            > int(vmstats[0].get("pswpout_pages", 0))
        ):
            flags.append("swap_activity")
    memory_psi = [
        float((((sample.get("psi") or {}).get("memory") or {}).get("full") or {}).get("avg10") or 0)
        for sample in samples
    ]
    if memory_psi and max(memory_psi) >= 1.0:
        flags.append("memory_pressure")
    cpu_psi = [
        float((((sample.get("psi") or {}).get("cpu") or {}).get("some") or {}).get("avg10") or 0)
        for sample in samples
    ]
    if cpu_psi and max(cpu_psi) >= 50.0:
        flags.append("cpu_pressure")
    counts = [
        int(sample["bench_descendant_process_count"])
        for sample in samples
        if sample.get("bench_descendant_process_count") is not None
    ]
    if len(counts) >= 2:
        growth_limit = max(8, math.ceil(max(1, counts[0]) * 0.2))
        if counts[-1] - counts[0] > growth_limit:
            flags.append("bench_process_growth")
    return {
        "polluted": bool(flags),
        "flags": sorted(set(flags)),
        "sample_count": len(samples),
        "started_at": samples[0].get("timestamp"),
        "ended_at": samples[-1].get("timestamp"),
        "max_bench_descendant_process_count": max(counts) if counts else None,
    }


class HostTelemetrySampler:
    def __init__(self, path: pathlib.Path, interval_s: float, root_pid: int) -> None:
        self.path = path
        self.interval_s = max(0.25, float(interval_s))
        self.root_pid = int(root_pid)
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: Any = None

    def _capture(self) -> None:
        sample = host_snapshot(self.root_pid)
        self.samples.append(sample)
        if self._handle is not None:
            self._handle.write(json.dumps(sample, sort_keys=True) + "\n")
            self._handle.flush()

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        self._capture()

        def loop() -> None:
            while not self._stop.wait(self.interval_s):
                self._capture()

        self._thread = threading.Thread(target=loop, name="abb-host-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_s * 2))
        self._capture()
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        return evaluate_host_pollution(self.samples)


def _cpu_governors() -> list[str]:
    governors: set[str] = set()
    for path in pathlib.Path("/sys/devices/system/cpu").glob(
        "cpu[0-9]*/cpufreq/scaling_governor"
    ):
        text = _read_text(path)
        if text:
            governors.add(text.strip())
    return sorted(governors)


def host_provenance(level: str = "full") -> dict[str, Any]:
    """Describe the host for run provenance.

    level="minimal" keeps only hardware/kernel facts and drops fields that
    fingerprint a particular deployment (cgroup path, CPU affinity/governors,
    procfs root). Release runs use minimal; resource-profile runs keep full
    detail because governors and affinity bear on measurement validity.
    """
    if level == "minimal":
        return {
            "provenance_level": "minimal",
            "kernel": platform.release(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "cgroup_version": 2 if (CGROUP_ROOT / "cgroup.controllers").exists() else None,
        }
    try:
        cpu_affinity = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpu_affinity = None
    current_cgroup = _current_cgroup()
    cpuset_effective = (
        _read_text(current_cgroup / "cpuset.cpus.effective")
        if current_cgroup is not None
        else None
    )
    return {
        "kernel": platform.release(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "cpu_affinity": cpu_affinity,
        "cpu_governors": _cpu_governors(),
        "cgroup_version": 2 if (CGROUP_ROOT / "cgroup.controllers").exists() else None,
        "cgroup_path": str(current_cgroup) if current_cgroup is not None else None,
        "cgroup_cpuset_effective": (
            cpuset_effective.strip() if cpuset_effective else None
        ),
        "procfs": str(PROC_ROOT),
    }


def _resource_value(row: dict[str, Any], name: str) -> float | int | None:
    resource = row.get("resource") or {}
    value = resource.get(name)
    return value if isinstance(value, (int, float)) else None


def duration_calibration(
    profiled_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    threshold_pct: float,
    *,
    profiled_manifest: dict[str, Any] | None = None,
    baseline_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    baseline = {
        (row.get("task_id"), row.get("engine"), int(row.get("attempt", 0))): row
        for row in baseline_rows
    }
    duration_deltas: list[float] = []
    collection_wall_deltas: list[float] = []
    by_engine_pairs: dict[str, dict[str, Any]] = {}
    status_mismatches = 0
    matched = 0
    for row in profiled_rows:
        key = (row.get("task_id"), row.get("engine"), int(row.get("attempt", 0)))
        other = baseline.get(key)
        if other is None:
            continue
        engine = str(row.get("engine") or "unknown")
        engine_pairs = by_engine_pairs.setdefault(
            engine,
            {
                "matched_attempts": 0,
                "status_mismatches": 0,
                "duration_deltas": [],
                "collection_wall_deltas": [],
            },
        )
        matched += 1
        engine_pairs["matched_attempts"] += 1
        if row.get("status") != other.get("status"):
            status_mismatches += 1
            engine_pairs["status_mismatches"] += 1
        current_ms = float(row.get("duration_ms") or 0)
        baseline_ms = float(other.get("duration_ms") or 0)
        if baseline_ms > 0:
            duration_delta = (current_ms / baseline_ms - 1.0) * 100.0
            duration_deltas.append(duration_delta)
            engine_pairs["duration_deltas"].append(duration_delta)
            collection_ms = (row.get("resource") or {}).get("collection_wall_ms")
            if isinstance(collection_ms, (int, float)):
                collection_delta = (
                    float(collection_ms) / baseline_ms - 1.0
                ) * 100.0
                collection_wall_deltas.append(collection_delta)
                engine_pairs["collection_wall_deltas"].append(collection_delta)
    median_delta = (
        float(statistics.median(duration_deltas)) if duration_deltas else None
    )
    p95_delta = _percentile(duration_deltas, 95)
    median_collection_delta = (
        float(statistics.median(collection_wall_deltas))
        if collection_wall_deltas
        else None
    )
    p95_collection_delta = _percentile(collection_wall_deltas, 95)
    mismatch_rate = status_mismatches / matched if matched else None
    calibration_by_engine: dict[str, Any] = {}
    engine_gates: list[bool] = []
    for engine, pairs in sorted(by_engine_pairs.items()):
        engine_matched = int(pairs["matched_attempts"])
        engine_status_mismatches = int(pairs["status_mismatches"])
        engine_duration = list(pairs["duration_deltas"])
        engine_collection = list(pairs["collection_wall_deltas"])
        engine_duration_median = (
            float(statistics.median(engine_duration)) if engine_duration else None
        )
        engine_collection_median = (
            float(statistics.median(engine_collection)) if engine_collection else None
        )
        engine_mismatch_rate = (
            engine_status_mismatches / engine_matched if engine_matched else None
        )
        engine_acceptable = bool(
            engine_matched
            and len(engine_duration) == engine_matched
            and len(engine_collection) == engine_matched
            and engine_duration_median is not None
            and engine_duration_median <= float(threshold_pct)
            and engine_collection_median is not None
            and engine_collection_median <= float(threshold_pct)
            and (engine_mismatch_rate or 0.0) <= 0.01
        )
        engine_gates.append(engine_acceptable)
        calibration_by_engine[engine] = {
            "matched_attempts": engine_matched,
            "duration_pairs": len(engine_duration),
            "collection_wall_pairs": len(engine_collection),
            "median_duration_delta_pct": engine_duration_median,
            "p95_duration_delta_pct": _percentile(engine_duration, 95),
            "median_collection_wall_delta_pct": engine_collection_median,
            "p95_collection_wall_delta_pct": _percentile(engine_collection, 95),
            "status_mismatches": engine_status_mismatches,
            "status_mismatch_rate": engine_mismatch_rate,
            "acceptable": engine_acceptable,
        }
    expected_calibration_engines = set(
        (profiled_manifest or {}).get("selected_engines")
        or [str(row.get("engine")) for row in profiled_rows]
    )
    complete_pairing = (
        matched == len(profiled_rows)
        and set(by_engine_pairs) == expected_calibration_engines
    )
    provenance_mismatches: list[str] = []
    provenance_checks: dict[str, bool] = {}
    if profiled_manifest is None or baseline_manifest is None:
        provenance_mismatches.append("run_manifest")
        provenance_checks["run_manifest"] = False
    else:
        task_signature = lambda manifest: sorted(
            (
                str(task.get("task_id")),
                str(task.get("sha256")),
            )
            for task in (manifest.get("resolved_tasks") or [])
            if isinstance(task, dict)
        )
        checks = {
            "corpus": task_signature(profiled_manifest)
            == task_signature(baseline_manifest),
            "seed": profiled_manifest.get("seed") == baseline_manifest.get("seed"),
            "k_runs": profiled_manifest.get("k_runs")
            == baseline_manifest.get("k_runs"),
            "selected_engines": sorted(profiled_manifest.get("selected_engines") or [])
            == sorted(baseline_manifest.get("selected_engines") or []),
            "score_mode": profiled_manifest.get("score_mode")
            == baseline_manifest.get("score_mode"),
            "jobs": (profiled_manifest.get("runner") or {}).get("jobs")
            == (baseline_manifest.get("runner") or {}).get("jobs"),
            "browser_reuse": (profiled_manifest.get("runner") or {}).get(
                "browser_reuse"
            )
            == (baseline_manifest.get("runner") or {}).get("browser_reuse"),
            "engine_pins": profiled_manifest.get("engines")
            == baseline_manifest.get("engines"),
            "harness_pins": (profiled_manifest.get("runner") or {}).get(
                "harness_pins"
            )
            == (baseline_manifest.get("runner") or {}).get("harness_pins"),
            "runner_source": bool(
                (profiled_manifest.get("runner") or {}).get("source")
            )
            and (profiled_manifest.get("runner") or {}).get("source")
            == (baseline_manifest.get("runner") or {}).get("source"),
            "host_provenance": profiled_manifest.get("host")
            == baseline_manifest.get("host"),
            "balanced_order": (
                (profiled_manifest.get("resource_profile") or {}).get("engine_order")
                == "balanced_rotation"
                and (baseline_manifest.get("resource_profile") or {}).get(
                    "engine_order"
                )
                == "balanced_rotation"
                and bool(
                    (profiled_manifest.get("resource_profile") or {}).get(
                        "engine_order_algorithm"
                    )
                )
                and (profiled_manifest.get("resource_profile") or {}).get(
                    "engine_order_algorithm"
                )
                == (baseline_manifest.get("resource_profile") or {}).get(
                    "engine_order_algorithm"
                )
            ),
            "profile_modes": (
                (profiled_manifest.get("resource_profile") or {}).get("mode")
                == "engine"
                and (baseline_manifest.get("resource_profile") or {}).get("mode")
                == "baseline"
            ),
        }
        provenance_checks.update(checks)
        provenance_mismatches.extend(
            name for name, matched_check in checks.items() if not matched_check
        )
    acceptable = bool(
        matched
        and complete_pairing
        and median_delta is not None
        and median_delta <= float(threshold_pct)
        and len(collection_wall_deltas) == matched
        and median_collection_delta is not None
        and median_collection_delta <= float(threshold_pct)
        and (mismatch_rate or 0.0) <= 0.01
        and bool(engine_gates)
        and all(engine_gates)
        and not provenance_mismatches
    )
    return {
        "matched_attempts": matched,
        "profiled_attempts": len(profiled_rows),
        "complete_pairing": complete_pairing,
        "duration_pairs": len(duration_deltas),
        "median_duration_delta_pct": median_delta,
        "p95_duration_delta_pct": p95_delta,
        "collection_wall_pairs": len(collection_wall_deltas),
        "median_collection_wall_delta_pct": median_collection_delta,
        "p95_collection_wall_delta_pct": p95_collection_delta,
        "status_mismatches": status_mismatches,
        "status_mismatch_rate": mismatch_rate,
        "by_engine": calibration_by_engine,
        "provenance_checks": provenance_checks,
        "provenance_mismatches": provenance_mismatches,
        "max_observer_effect_pct": float(threshold_pct),
        "acceptable": acceptable,
    }


def _linear_slope(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    xs = list(range(len(values)))
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(values)
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / denom


def summarize_resources(
    run_manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    host_summary: dict[str, Any] | None,
    calibration: dict[str, Any] | None,
    cold_starts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    expected_engines = tuple(
        dict.fromkeys(
            str(engine)
            for engine in (run_manifest.get("selected_engines") or [])
            if str(engine)
        )
    )
    grouped: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((str(row.get("task_id")), int(row.get("attempt", 0))), {})[
            str(row.get("engine"))
        ] = row
    intersection_keys = [
        key
        for key, engine_rows in grouped.items()
        if all(
            engine in engine_rows and engine_rows[engine].get("status") == "pass"
            for engine in expected_engines
        )
    ]
    intersection_rows = {
        engine: [grouped[key][engine] for key in intersection_keys]
        for engine in expected_engines
    }

    excluded_status: dict[str, dict[str, int]] = {
        engine: {} for engine in expected_engines
    }
    excluded_attempts = 0
    for key, engine_rows in grouped.items():
        if key in intersection_keys:
            continue
        excluded_attempts += 1
        for engine in expected_engines:
            status = str((engine_rows.get(engine) or {}).get("status") or "missing")
            excluded_status[engine][status] = excluded_status[engine].get(status, 0) + 1
    functional_outcomes: dict[str, Any] = {}
    quality_flags_by_engine: dict[str, dict[str, int]] = {
        engine: {} for engine in expected_engines
    }
    for engine in expected_engines:
        engine_rows = [row for row in rows if str(row.get("engine")) == engine]
        status_counts: dict[str, int] = {}
        for row in engine_rows:
            status = str(row.get("status") or "missing")
            status_counts[status] = status_counts.get(status, 0) + 1
        pass_count = status_counts.get("pass", 0)
        functional_outcomes[engine] = {
            "attempts": len(engine_rows),
            "pass": pass_count,
            "pass_rate": pass_count / len(engine_rows) if engine_rows else None,
            "status_counts": status_counts,
        }
        for row in engine_rows:
            for flag in (row.get("resource") or {}).get("quality_flags") or []:
                name = str(flag)
                quality_flags_by_engine[engine][name] = (
                    quality_flags_by_engine[engine].get(name, 0) + 1
                )

    metrics = (
        "cpu_total_ms",
        "cpu_user_ms",
        "cpu_system_ms",
        "avg_cores",
        "pss_baseline_bytes",
        "pss_peak_bytes",
        "pss_end_bytes",
        "pss_peak_delta_bytes",
        "process_count_peak",
        "fixture_app_rx_body_bytes",
        "fixture_app_tx_body_bytes",
    )

    def metric_value(row: dict[str, Any], metric: str) -> float | int | None:
        if metric.startswith("fixture_"):
            value = (
                ((row.get("resource") or {}).get("fixture_traffic") or {}).get(metric)
            )
            return value if isinstance(value, (int, float)) else None
        return _resource_value(row, metric)

    def metric_stats_for_rows(engine_rows: list[dict[str, Any]]) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        for metric in metrics:
            values = [
                value
                for row in engine_rows
                if (value := metric_value(row, metric)) is not None
            ]
            stats[metric] = _metric_stats(values)
        return stats

    by_engine: dict[str, Any] = {}
    missing_intersection_metrics: list[str] = []
    for engine in expected_engines:
        engine_rows = intersection_rows[engine]
        for row in engine_rows:
            traffic = (row.get("resource") or {}).get("fixture_traffic") or {}
            if traffic.get("available") is not True:
                missing_intersection_metrics.append(
                    f"{row.get('task_id')}#{row.get('attempt')}:{engine}:fixture_traffic"
                )
        for metric in metrics:
            for row in engine_rows:
                if metric_value(row, metric) is None:
                    missing_intersection_metrics.append(
                        f"{row.get('task_id')}#{row.get('attempt')}:{engine}:{metric}"
                    )
        pss_end_values = [
            float(value)
            for value in (
                _resource_value(row, "pss_end_bytes") for row in engine_rows
            )
            if value is not None
        ]
        process_values = [
            float(value)
            for value in (
                _resource_value(row, "process_count_end") for row in engine_rows
            )
            if value is not None
        ]
        by_engine[engine] = {
            "intersection_attempts": len(engine_rows),
            "metrics": metric_stats_for_rows(engine_rows),
            "leak_curve": {
                "pss_end_slope_bytes_per_attempt": _linear_slope(pss_end_values),
                "process_count_end_slope_per_attempt": _linear_slope(process_values),
            },
        }

    task_meta = {
        str(task.get("task_id")): task
        for task in (run_manifest.get("resolved_tasks") or [])
        if isinstance(task, dict)
    }
    dimension_rows: dict[
        str, dict[str, dict[str, list[dict[str, Any]]]]
    ] = {
        dimension: {}
        for dimension in ("layer", "subset", "driver", "family")
    }
    for key in intersection_keys:
        reference = grouped[key][expected_engines[0]]
        meta = task_meta.get(str(reference.get("task_id"))) or {}
        dimension_values = {
            "layer": [str(reference.get("layer") or meta.get("layer") or "unknown")],
            "subset": [
                str(reference.get("subset_id") or meta.get("subset_id") or "unknown")
            ],
            "driver": [str(reference.get("driver") or meta.get("driver") or "unknown")],
            "family": sorted(
                {
                    str(tag)
                    for tag in (meta.get("tags") or [])
                    if str(tag).startswith("family.")
                }
            )
            or ["unclassified"],
        }
        for dimension, values in dimension_values.items():
            for value in values:
                bucket = dimension_rows[dimension].setdefault(
                    value, {engine: [] for engine in expected_engines}
                )
                for engine in expected_engines:
                    bucket[engine].append(grouped[key][engine])

    stratified: dict[str, Any] = {}
    for dimension, groups in dimension_rows.items():
        stratified[dimension] = {}
        for group_name in sorted(groups):
            engine_groups = groups[group_name]
            stratified[dimension][group_name] = {
                "intersection_attempts": len(engine_groups[expected_engines[0]]),
                "by_engine": {
                    engine: {
                        "metrics": metric_stats_for_rows(engine_groups[engine]),
                    }
                    for engine in expected_engines
                },
            }

    profile = run_manifest.get("resource_profile") or {}
    runner = run_manifest.get("runner") or {}
    reasons: list[str] = []
    if profile.get("mode") != "engine":
        reasons.append("engine resource profile not enabled")
    if len(expected_engines) < 2:
        reasons.append("resource comparison requires at least two selected engines")
    if int(runner.get("jobs") or 0) != 1:
        reasons.append("resource comparison requires jobs=1")
    if int(run_manifest.get("k_runs") or 0) < 5:
        reasons.append("resource comparison requires k>=5")
    if str(run_manifest.get("score_mode")) != "independent":
        reasons.append("resource comparison requires independent mode for balanced engine order")
    if not intersection_keys:
        reasons.append("all-pass intersection is empty")
    if missing_intersection_metrics:
        reasons.append("one or more intersection resource metrics are unavailable")
    if not host_summary or host_summary.get("polluted"):
        reasons.append("host telemetry pollution gate failed")
    if not calibration:
        reasons.append("profiler on/off calibration is missing")
    elif not calibration.get("acceptable"):
        reasons.append("profiler observer effect exceeds the configured gate")

    cold_by_engine: dict[str, list[dict[str, Any]]] = {
        engine: [] for engine in expected_engines
    }
    for item in cold_starts or []:
        engine = str(item.get("engine"))
        if engine in cold_by_engine:
            cold_by_engine[engine].append(item)
    cold_summary_by_engine: dict[str, Any] = {}
    for engine, samples in cold_by_engine.items():
        cold_summary_by_engine[engine] = {
            "samples": len(samples),
            "metrics": {
                metric: _metric_stats(
                    [
                        value
                        for sample in samples
                        if isinstance(
                            (value := sample.get(metric)),
                            (int, float),
                        )
                    ]
                )
                for metric in (
                    "ready_ms",
                    "launch_cpu_ms",
                    "launch_peak_pss_bytes",
                )
            },
        }

    return {
        "schema": RESOURCE_SUMMARY_SCHEMA,
        "run_id": run_manifest.get("run_id"),
        "engines": list(expected_engines),
        "resource_comparison_eligible": not reasons,
        "ineligibility_reasons": reasons,
        "comparison_contract": {
            "scope": "warm engine_scope attempts",
            "population": "same task_id+attempt where every selected engine passes",
            "functional_score_combined": False,
            "engine_order": profile.get("engine_order"),
        },
        "all_pass_intersection": {
            "attempts": len(intersection_keys),
            "keys": [
                {"task_id": task_id, "attempt": attempt}
                for task_id, attempt in intersection_keys
            ],
        },
        "excluded": {
            "task_attempts": excluded_attempts,
            "status_by_engine": excluded_status,
        },
        "functional_outcomes": functional_outcomes,
        "quality_flags_by_engine": quality_flags_by_engine,
        "by_engine": by_engine,
        "stratified": stratified,
        "cold_start": {
            "separate_from_warm_aggregation": True,
            "samples_by_engine": cold_by_engine,
            "by_engine": cold_summary_by_engine,
        },
        "host_quality": host_summary,
        "observer_effect": calibration,
        "missing_intersection_metrics": missing_intersection_metrics[:100],
        "provenance": {
            "jobs": runner.get("jobs"),
            "browser_reuse": runner.get("browser_reuse"),
            "k_runs": run_manifest.get("k_runs"),
            "sample_interval_ms": profile.get("sample_interval_ms"),
            "host": run_manifest.get("host"),
            "engines": run_manifest.get("engines"),
            "harness_pins": runner.get("harness_pins"),
        },
    }


def write_resource_card(path: pathlib.Path, summary: dict[str, Any]) -> None:
    lines = [
        f"# Resource card: {summary.get('run_id')}",
        "",
        f"- resource_comparison_eligible: `{summary.get('resource_comparison_eligible')}`",
        f"- all-pass intersection: `{summary.get('all_pass_intersection', {}).get('attempts', 0)}` task-attempts",
        "- scope: warm engine process tree; cold-start results are separate",
        "- functional capability scores are not combined with resource cost",
    ]
    reasons = summary.get("ineligibility_reasons") or []
    if reasons:
        lines.extend(["", "## Eligibility", ""])
        lines.extend(f"- {reason}" for reason in reasons)
    lines.extend(
        [
            "",
            "## All-pass intersection metrics",
            "",
            "| engine | functional pass/attempts | intersection | CPU median ms | CPU p95 ms | PSS peak median MiB | PSS delta median MiB | fixture tx median bytes |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for engine in summary.get("engines") or []:
        payload = (summary.get("by_engine") or {}).get(engine) or {}
        functional = (summary.get("functional_outcomes") or {}).get(engine) or {}
        stats = payload.get("metrics") or {}

        def median(name: str) -> float | None:
            value = (stats.get(name) or {}).get("median")
            return float(value) if isinstance(value, (int, float)) else None

        def show(value: float | None, divisor: float = 1.0) -> str:
            return "unavailable" if value is None else f"{value / divisor:.2f}"

        cpu = stats.get("cpu_total_ms") or {}
        lines.append(
            f"| {engine} | {functional.get('pass', 0)}/{functional.get('attempts', 0)} | "
            f"{payload.get('intersection_attempts', 0)} | "
            f"{show(median('cpu_total_ms'))} | "
            f"{show(float(cpu['p95']) if isinstance(cpu.get('p95'), (int, float)) else None)} | "
            f"{show(median('pss_peak_bytes'), 1024 * 1024)} | "
            f"{show(median('pss_peak_delta_bytes'), 1024 * 1024)} | "
            f"{show(median('fixture_app_tx_body_bytes'))} |"
        )
    lines.extend(
        [
            "",
            "## Cold start (separate diagnostic)",
            "",
            "| engine | samples | ready median ms | launch CPU median ms | launch peak PSS median MiB |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for engine in summary.get("engines") or []:
        payload = (
            (summary.get("cold_start") or {}).get("by_engine", {}).get(engine)
            or {}
        )
        stats = payload.get("metrics") or {}

        def cold_median(name: str) -> float | None:
            value = (stats.get(name) or {}).get("median")
            return float(value) if isinstance(value, (int, float)) else None

        ready = cold_median("ready_ms")
        launch_cpu = cold_median("launch_cpu_ms")
        launch_pss = cold_median("launch_peak_pss_bytes")
        lines.append(
            f"| {engine} | {payload.get('samples', 0)} | "
            f"{'unavailable' if ready is None else f'{ready:.2f}'} | "
            f"{'unavailable' if launch_cpu is None else f'{launch_cpu:.2f}'} | "
            f"{'unavailable' if launch_pss is None else f'{launch_pss / (1024 * 1024):.2f}'} |"
        )
    lines.extend(["", "## Excluded outcomes", ""])
    lines.append("| engine | status counts outside all-pass intersection |")
    lines.append("|---|---|")
    for engine, counts in (summary.get("excluded", {}).get("status_by_engine") or {}).items():
        text = ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"
        lines.append(f"| {engine} | {text} |")
    lines.extend(["", "## Measurement quality flags", ""])
    for engine, counts in (summary.get("quality_flags_by_engine") or {}).items():
        text = ", ".join(
            f"{key}={value}" for key, value in sorted(counts.items())
        ) or "none"
        lines.append(f"- {engine}: `{text}`")
    lines.extend(["", "## Host and observer quality", ""])
    host = summary.get("host_quality") or {}
    observer = summary.get("observer_effect") or {}
    lines.append(f"- host pollution flags: `{', '.join(host.get('flags') or []) or 'none'}`")
    lines.append(
        f"- profiler median duration delta: `{observer.get('median_duration_delta_pct')}`%; "
        f"collection-wall delta: `{observer.get('median_collection_wall_delta_pct')}`%; "
        f"status mismatches: `{observer.get('status_mismatches')}`"
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
