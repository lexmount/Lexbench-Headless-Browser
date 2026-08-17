"""CDP stable-surface coverage accounting.

This module turns the machine-readable Chrome DevTools Protocol definition
(`devtools-protocol`'s `browser_protocol.json` + `js_protocol.json`, pinned via
`harness_pins.json`) into a coverage ledger for the benchmark's `cdp.*` feature
tags.

Every *stable* protocol member (a command or event that is neither
`experimental` nor `deprecated`, in a domain that is itself neither experimental
nor deprecated) is placed into exactly one of three states:

  * ``tested``  — at least one task carries a feature tag that maps to it.
  * ``waived``  — it is listed in ``config/cdp_waivers.json`` with a
                  category and reason (hardware-bound / Chrome-private /
                  security-domain / not-applicable).
  * ``gap``     — neither of the above; this is an out-standing target to cover.

The acceptance bar is ``gap == 0`` with the live waiver set matching the frozen
whitelist (``FROZEN_WAIVERS``) member-for-member, each waiver justified.

Feature ↔ member matching uses a *squeeze key*: lower-case with every
non-alphanumeric character removed, computed independently for the domain and
the member name. This absorbs the historical snake-case irregularities in the
dataset (``domsnapshot`` vs ``dom_snapshot``, ``javascript`` vs
``java_script``) without a hand-maintained alias table, and is verified
collision-free against the pinned protocol at load time.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any


BENCH_ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS_PINS_PATH = BENCH_ROOT / "harness_pins.json"
DEFAULT_PROTOCOL_DIR = BENCH_ROOT / "node_modules" / "devtools-protocol" / "json"
DEFAULT_TASKS_DIR = BENCH_ROOT / "tasks"
DEFAULT_WAIVERS_PATH = BENCH_ROOT / "config" / "cdp_waivers.json"
DEFAULT_COVERAGE_MD = BENCH_ROOT / "generated" / "cdp_coverage.md"
DEFAULT_COVERAGE_JSON = BENCH_ROOT / "generated" / "cdp_coverage.json"

PROTOCOL_FILES = ("browser_protocol.json", "js_protocol.json")

# Every waiver must declare one of these justification categories.
WAIVER_CATEGORIES = {
    "hardware": "requires physical hardware / OS capability a headless engine cannot own",
    "chrome_private": "Chrome-internal behaviour with no cross-implementation contract",
    "security_domain": "security/permission surface not applicable to the bench sandbox",
    "not_applicable": "structurally out of the benchmark's protocol-observation scope",
}

# Frozen waiver ledger. The acceptance bar uses this fixed whitelist of
# structurally untestable stable members. Any addition or removal is a
# `coverage --check` failure until the policy and whitelist are updated together.
FROZEN_WAIVERS = frozenset({
    "Browser.close",
    "Browser.addPrivacySandboxCoordinatorKeyConfig",
    "Browser.addPrivacySandboxEnrollmentOverride",
    "Debugger.setScriptSource",
    "Network.webTransportCreated",
    "Network.webTransportConnectionEstablished",
    "Network.webTransportClosed",
    "Page.interstitialShown",
    "Page.interstitialHidden",
    "Performance.metrics",
    "Runtime.exceptionRevoked",
    "Runtime.inspectRequested",
    "Target.receivedMessageFromTarget",
    "Target.targetCrashed",
})

# Feature tags under cdp.* that intentionally do not resolve to a *current*
# stable protocol member. These are meta-features and negative-lane probes that
# deliberately exercise unknown / removed / deprecated commands; the whole point
# is that they name something the stable surface no longer offers. Listing them
# here keeps the "unmapped feature == real typo" signal clean.
SYNTHETIC_CDP_FEATURES = {
    "cdp.protocol.unknown_method",       # synthetic: unknown-method JSON-RPC error probe
    "cdp.page.get_cookies",              # negative lane: Page.getCookies was removed from the protocol
    "cdp.page.delete_cookie.removed",    # negative lane: Page.deleteCookie is experimental+deprecated
}

# Stable members that benchmark policy has removed as targets (PR #46 dropped
# screenshot/PDF). Their feature tags are rejected by the manifest validator
# (NON_TARGET_FEATURES), so no task can ever cover them. They are accounted in a
# dedicated `out_of_scope` state — excluded from both the gap count and the
# waiver ledger — rather than sitting in `gap` forever. This mirrors
# runner.run.NON_TARGET_CDP_METHODS.
OUT_OF_SCOPE_MEMBERS = {
    "Page.captureScreenshot": "screenshot removed as a benchmark target (PR #46); diagnostic-only surface",
    "Page.printToPDF": "PDF removed as a benchmark target (PR #46); diagnostic-only surface",
}


def squeeze(text: str) -> str:
    """Collapse a name to its alphanumeric lower-case skeleton."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass(frozen=True)
class Member:
    domain: str
    name: str
    kind: str  # "command" | "event"
    stable: bool
    reason: str  # "" when stable, else why it is excluded

    @property
    def id(self) -> str:
        return f"{self.domain}.{self.name}"

    @property
    def key(self) -> str:
        return f"{squeeze(self.domain)}|{squeeze(self.name)}"


@dataclass
class MemberCoverage:
    member: Member
    state: str  # "tested" | "waived" | "gap"
    task_ids: list[str] = field(default_factory=list)
    waiver: dict[str, Any] | None = None


@dataclass
class CoverageResult:
    protocol_version: str
    protocol_revision: str
    members: list[MemberCoverage]
    synthetic_features: list[str]
    unmapped_features: list[str]  # cdp.* features that map to no member at all
    waiver_errors: list[str]

    @property
    def stable_total(self) -> int:
        return len(self.members)

    def by_state(self, state: str) -> list[MemberCoverage]:
        return [m for m in self.members if m.state == state]

    @property
    def tested(self) -> list[MemberCoverage]:
        return self.by_state("tested")

    @property
    def waived(self) -> list[MemberCoverage]:
        return self.by_state("waived")

    @property
    def gap(self) -> list[MemberCoverage]:
        return self.by_state("gap")

    @property
    def out_of_scope(self) -> list[MemberCoverage]:
        return self.by_state("out_of_scope")

    @property
    def waiver_fraction(self) -> float:
        return (len(self.waived) / self.stable_total) if self.stable_total else 0.0

    @property
    def waived_ids(self) -> set[str]:
        return {mc.member.id for mc in self.waived}

    @property
    def waivers_added(self) -> list[str]:
        """Waivers present in the ledger but not in the frozen set."""
        return sorted(self.waived_ids - FROZEN_WAIVERS)

    @property
    def waivers_missing(self) -> list[str]:
        """Frozen waivers absent from the live ledger."""
        return sorted(FROZEN_WAIVERS - self.waived_ids)


def protocol_pin() -> dict[str, Any]:
    """Return the devtools-protocol pin record from harness_pins.json (or {})."""
    if not HARNESS_PINS_PATH.exists():
        return {}
    try:
        pins = json.loads(HARNESS_PINS_PATH.read_text())
    except Exception:
        return {}
    return (pins.get("protocols") or {}).get("devtools-protocol") or {}


def load_protocol(protocol_dir: pathlib.Path = DEFAULT_PROTOCOL_DIR) -> tuple[list[Member], str]:
    """Load every protocol member (stable and not) plus a version string.

    Raises ValueError if the squeeze keys are not collision-free within the
    stable universe, which would make feature matching ambiguous.
    """
    members: list[Member] = []
    version = "unknown"
    for fname in PROTOCOL_FILES:
        path = protocol_dir / fname
        if not path.exists():
            raise FileNotFoundError(f"protocol definition not found: {path}")
        doc = json.loads(path.read_text())
        ver = doc.get("version") or {}
        if isinstance(ver, dict) and ver.get("major"):
            version = f"{ver['major']}.{ver['minor']}"
        for dom in doc.get("domains", []):
            domain = dom["domain"]
            dom_unstable = bool(dom.get("experimental") or dom.get("deprecated"))
            dom_reason = _reason(dom, prefix="domain")
            for group, kind in (("commands", "command"), ("events", "event")):
                for item in dom.get(group, []):
                    item_unstable = bool(item.get("experimental") or item.get("deprecated"))
                    stable = not (dom_unstable or item_unstable)
                    reason = dom_reason or _reason(item, prefix="member")
                    members.append(
                        Member(domain=domain, name=item["name"], kind=kind, stable=stable, reason=reason)
                    )
    _assert_no_key_collision(members)
    return members, version


def _reason(node: dict[str, Any], prefix: str) -> str:
    flags = []
    if node.get("experimental"):
        flags.append("experimental")
    if node.get("deprecated"):
        flags.append("deprecated")
    return f"{prefix}:{'+'.join(flags)}" if flags else ""


def _assert_no_key_collision(members: list[Member]) -> None:
    seen: dict[str, str] = {}
    for m in members:
        if not m.stable:
            continue
        if m.key in seen and seen[m.key] != m.id:
            raise ValueError(
                f"squeeze-key collision between stable members {seen[m.key]} and {m.id} "
                f"(key={m.key}); feature matching would be ambiguous"
            )
        seen[m.key] = m.id


def feature_squeeze_key(feature: str) -> str | None:
    """Map a cdp.<domain>.<member...> feature tag to a member squeeze key."""
    parts = feature.split(".")
    if len(parts) < 3 or parts[0] != "cdp":
        return None
    domain = parts[1]
    member = "".join(parts[2:])
    return f"{squeeze(domain)}|{squeeze(member)}"


def collect_task_cdp_features(tasks_dir: pathlib.Path = DEFAULT_TASKS_DIR) -> dict[str, list[str]]:
    """Return {cdp.* feature -> [task_id, ...]} across all task JSON files."""
    out: dict[str, list[str]] = {}
    for path in sorted(tasks_dir.rglob("*.json")):
        try:
            task = json.loads(path.read_text())
        except Exception:
            continue
        task_id = task.get("task_id") or path.stem
        for feature in task.get("features", []):
            if isinstance(feature, str) and feature.startswith("cdp."):
                out.setdefault(feature, []).append(task_id)
    return out


def load_waivers(path: pathlib.Path = DEFAULT_WAIVERS_PATH) -> dict[str, dict[str, Any]]:
    """Load the waiver ledger: {member_id -> {category, reason, ...}}."""
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    entries = doc.get("waivers", doc) if isinstance(doc, dict) else doc
    out: dict[str, dict[str, Any]] = {}
    if isinstance(entries, dict):
        for member_id, spec in entries.items():
            out[member_id] = spec if isinstance(spec, dict) else {"reason": str(spec)}
    elif isinstance(entries, list):
        for spec in entries:
            if isinstance(spec, dict) and spec.get("member"):
                out[spec["member"]] = spec
    return out


def build_coverage(
    protocol_dir: pathlib.Path = DEFAULT_PROTOCOL_DIR,
    tasks_dir: pathlib.Path = DEFAULT_TASKS_DIR,
    waivers_path: pathlib.Path = DEFAULT_WAIVERS_PATH,
) -> CoverageResult:
    members, version = load_protocol(protocol_dir)
    stable_by_key: dict[str, Member] = {m.key: m for m in members if m.stable}
    all_by_key: dict[str, Member] = {}
    for m in members:
        # Prefer stable member if a stable/unstable pair ever shared a key
        # (does not happen today; defensive).
        all_by_key.setdefault(m.key, m)
        if m.stable:
            all_by_key[m.key] = m

    features = collect_task_cdp_features(tasks_dir)
    waivers = load_waivers(waivers_path)

    tested_tasks: dict[str, list[str]] = {}
    synthetic: list[str] = []
    unmapped: list[str] = []
    for feature, task_ids in features.items():
        key = feature_squeeze_key(feature)
        if key and key in stable_by_key:
            tested_tasks.setdefault(stable_by_key[key].id, []).extend(task_ids)
        elif key and key in all_by_key:
            # Maps to a real but non-stable member: legitimate, just not part of
            # the stable denominator. Not a gap, not an error.
            continue
        elif feature in SYNTHETIC_CDP_FEATURES:
            synthetic.append(feature)
        else:
            unmapped.append(feature)

    waiver_errors = _validate_waivers(waivers, stable_by_key, tested_tasks)

    coverage: list[MemberCoverage] = []
    for m in sorted((m for m in members if m.stable), key=lambda x: (x.domain, x.name)):
        if m.id in OUT_OF_SCOPE_MEMBERS:
            coverage.append(
                MemberCoverage(
                    member=m,
                    state="out_of_scope",
                    waiver={"category": "not_applicable", "reason": OUT_OF_SCOPE_MEMBERS[m.id]},
                )
            )
        elif m.id in tested_tasks:
            coverage.append(
                MemberCoverage(member=m, state="tested", task_ids=sorted(set(tested_tasks[m.id])))
            )
        elif m.id in waivers:
            coverage.append(MemberCoverage(member=m, state="waived", waiver=waivers[m.id]))
        else:
            coverage.append(MemberCoverage(member=m, state="gap"))

    pin = protocol_pin()
    return CoverageResult(
        protocol_version=version,
        protocol_revision=str(pin.get("version") or "unpinned"),
        members=coverage,
        synthetic_features=sorted(set(synthetic)),
        unmapped_features=sorted(set(unmapped)),
        waiver_errors=waiver_errors,
    )


def _validate_waivers(
    waivers: dict[str, dict[str, Any]],
    stable_by_key: dict[str, Member],
    tested_tasks: dict[str, list[str]],
) -> list[str]:
    errors: list[str] = []
    stable_ids = {m.id for m in stable_by_key.values()}
    for member_id, spec in sorted(waivers.items()):
        if member_id not in stable_ids:
            errors.append(f"waiver for `{member_id}` does not name a stable protocol member")
            continue
        if member_id in tested_tasks:
            errors.append(f"waiver for `{member_id}` is redundant: the member is already tested")
        category = spec.get("category")
        if category not in WAIVER_CATEGORIES:
            errors.append(
                f"waiver for `{member_id}` has category `{category}` "
                f"(allowed: {', '.join(sorted(WAIVER_CATEGORIES))})"
            )
        if not str(spec.get("reason") or "").strip():
            errors.append(f"waiver for `{member_id}` is missing a non-empty reason")
    return errors


def render_json(result: CoverageResult) -> dict[str, Any]:
    return {
        "generated_from": "runner/coverage.py",
        "protocol_version": result.protocol_version,
        "protocol_revision": result.protocol_revision,
        "summary": {
            "stable_total": result.stable_total,
            "tested": len(result.tested),
            "waived": len(result.waived),
            "out_of_scope": len(result.out_of_scope),
            "gap": len(result.gap),
            "waiver_fraction_pct": round(result.waiver_fraction * 100, 3),
        },
        "gap": [m.member.id for m in result.gap],
        "waived": [
            {"member": m.member.id, **(m.waiver or {})} for m in result.waived
        ],
        "out_of_scope": [
            {"member": m.member.id, **(m.waiver or {})} for m in result.out_of_scope
        ],
        "tested": {m.member.id: m.task_ids for m in result.tested},
        "synthetic_features": result.synthetic_features,
        "unmapped_features": result.unmapped_features,
    }


def render_markdown(result: CoverageResult) -> str:
    lines: list[str] = []
    a = lines.append
    a("# CDP stable-surface coverage matrix")
    a("")
    a("> Generated by `python3 -m runner.run coverage`. Do not edit by hand.")
    a(">")
    a(
        f"> Source: `devtools-protocol` pin `{result.protocol_revision}` "
        f"(protocol {result.protocol_version}), stable = non-experimental & non-deprecated."
    )
    a("")
    total = result.stable_total or 1
    a("## Summary")
    a("")
    a("| state | count | share |")
    a("|---|---:|---:|")
    a(f"| tested | {len(result.tested)} | {len(result.tested)/total*100:.1f}% |")
    a(f"| waived | {len(result.waived)} | {result.waiver_fraction*100:.1f}% |")
    a(f"| out_of_scope | {len(result.out_of_scope)} | {len(result.out_of_scope)/total*100:.1f}% |")
    a(f"| **gap** | **{len(result.gap)}** | **{len(result.gap)/total*100:.1f}%** |")
    a(f"| stable total | {result.stable_total} | 100% |")
    a("")
    frozen_ok = not result.waivers_added and not result.waivers_missing
    accept = "PASS" if not result.gap and not result.waiver_errors and frozen_ok else "OPEN"
    a(
        f"Acceptance (`gap == 0`, waiver set matches the frozen "
        f"whitelist, no waiver errors): **{accept}**"
    )
    a("")

    if result.gap:
        a("## Gap (untested stable members — outstanding targets)")
        a("")
        by_domain: dict[str, list[str]] = {}
        for m in result.gap:
            by_domain.setdefault(m.member.domain, []).append(m.member.name)
        a("| domain | count | members |")
        a("|---|---:|---|")
        for domain in sorted(by_domain):
            names = sorted(by_domain[domain])
            a(f"| {domain} | {len(names)} | {', '.join(names)} |")
        a("")

    a("## Waived")
    a("")
    if result.waived:
        a("| member | category | reason |")
        a("|---|---|---|")
        for m in result.waived:
            w = m.waiver or {}
            a(f"| `{m.member.id}` | {w.get('category', '?')} | {w.get('reason', '')} |")
    else:
        a("_No waivers._")
    a("")

    a("## Out of scope (benchmark-policy exclusions, not counted as gap or waiver)")
    a("")
    if result.out_of_scope:
        a("| member | reason |")
        a("|---|---|")
        for m in result.out_of_scope:
            a(f"| `{m.member.id}` | {(m.waiver or {}).get('reason', '')} |")
    else:
        a("_None._")
    a("")

    if result.waiver_errors:
        a("## Waiver ledger errors")
        a("")
        for err in result.waiver_errors:
            a(f"- {err}")
        a("")

    a("## Tested")
    a("")
    a("<details><summary>All tested stable members and the tasks that cover them</summary>")
    a("")
    a("| member | kind | tasks |")
    a("|---|---|---|")
    for m in result.tested:
        a(f"| `{m.member.id}` | {m.member.kind} | {', '.join(m.task_ids)} |")
    a("")
    a("</details>")
    a("")

    if result.unmapped_features:
        a("## Unmapped `cdp.*` features")
        a("")
        a("These feature tags do not resolve to any protocol member. Fix the tag or")
        a("register it in `SYNTHETIC_CDP_FEATURES` if it is an intentional meta-feature.")
        a("")
        for feature in result.unmapped_features:
            a(f"- `{feature}`")
        a("")
    return "\n".join(lines) + "\n"


def write_reports(
    result: CoverageResult,
    md_path: pathlib.Path = DEFAULT_COVERAGE_MD,
    json_path: pathlib.Path = DEFAULT_COVERAGE_JSON,
) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(result))
    json_path.write_text(json.dumps(render_json(result), indent=2, sort_keys=True) + "\n")
