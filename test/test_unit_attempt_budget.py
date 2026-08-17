"""Guards against harness overhead being charged to the engine's task budget.

`timeouts.task_ms` (30s by default) is an absolute wall-clock budget: the runner
wraps the whole adapter process in `subprocess.run(timeout=...)`, not just the
part where the engine is doing work. So any fixed per-attempt cost that has
nothing to do with the engine eats into the engine's share of that budget, and
when the cost is large enough the harness's own defect is recorded as an engine
`timeout` — a well-formed result row with a completely wrong conclusion.

Two shapes of that bug have shipped already (see the v0.4 run):

* a `Promise.race` deadline timer that is neither cleared nor unref'd keeps the
  node event loop alive after the result has been written to stdout, so every
  attempt burns the full budget before the process exits;
* launching an adapter through `cargo run` / `go run` pays build-system cost per
  attempt — cargo serializes every invocation on a global package-cache lock,
  which at --jobs 64 is ~15s of pure queueing.

The tell for both is that the median duration barely differs between engines:
engine capability cannot produce that coincidence, so the time is not the
engine's. These tests encode the two invariants directly.
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest

from runner import run as runner_run

SCRIPTS_DIR = runner_run.BENCH_ROOT / "runner" / "scripts"

# Programs that compile-then-run. Fine at a developer's prompt, not once per
# attempt inside a timed benchmark.
BUILD_WRAPPERS = {"cargo", "go", "npx", "uvx", "mvn", "gradle", "dotnet"}


def js_sources() -> list[pathlib.Path]:
    return sorted(SCRIPTS_DIR.rglob("*.js"))


def test_js_sources_are_discoverable():
    # A silent glob miss would turn the lint below into a no-op.
    assert len(js_sources()) >= 5


def test_no_adapter_launches_through_a_build_wrapper():
    offenders = []
    for kind, spec in runner_run.SCENARIO_ADAPTER_KINDS.items():
        program = pathlib.PurePosixPath(spec["argv"][0]).name
        if program in BUILD_WRAPPERS:
            offenders.append(f"{kind}: argv starts with `{program}`")
    assert not offenders, (
        "adapters must be launched as a pre-built binary, not through a build wrapper "
        "(the build cost is charged to timeouts.task_ms): " + "; ".join(offenders)
    )


def test_compiled_adapters_declare_a_build_command():
    # doctor names the command when a binary is missing; a compiled adapter with
    # no hint would report `missing_build` and leave the reader stuck.
    for driver_key in runner_run.compiled_adapter_binaries():
        assert driver_key in runner_run.COMPILED_ADAPTER_BUILD_HINTS, (
            f"compiled adapter `{driver_key}` has no entry in COMPILED_ADAPTER_BUILD_HINTS"
        )


def test_compiled_adapter_binaries_are_detected():
    # Shape-based detection: the Go and Rust adapters must be seen as compiled,
    # and the interpreted ones must not.
    detected = runner_run.compiled_adapter_binaries()
    assert {"chromedp", "rod", "chromiumoxide"} <= set(detected)
    assert "selenium" not in detected and "pydoll" not in detected


def test_compiled_adapter_binaries_rejects_a_duplicate_driver_key(monkeypatch):
    # A collision must fail loudly: silently keeping the last entry would drop a
    # binary from the doctor check with nothing to show for it.
    from runner.run import BenchError

    clash = dict(runner_run.SCENARIO_ADAPTER_KINDS)
    clash["thin_rod_copy"] = {"driver_key": "rod", "argv": ["{script}/rod_adapter"], "script": "x"}
    monkeypatch.setattr(runner_run, "SCENARIO_ADAPTER_KINDS", clash)
    with pytest.raises(BenchError, match="duplicate driver_key `rod`"):
        runner_run.compiled_adapter_binaries()


def test_check_compiled_adapters_fails_when_a_binary_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner_run,
        "compiled_adapter_binaries",
        lambda: {"chromiumoxide": tmp_path / "definitely-not-built"},
    )
    assert runner_run.check_compiled_adapters(emit=False) is False


def build_fake_adapter(tmp_path, source_newer: bool):
    """A Go-shaped adapter tree whose binary is older or newer than its source."""
    root = tmp_path / "rod_adapter"
    root.mkdir()
    manifest = root / "go.mod"
    manifest.write_text("module fake\n")
    source = root / "main.go"
    source.write_text("package main\n")
    binary = root / "rod_adapter"
    binary.write_text("#!/bin/true\n")
    old, new = 1_000_000, 2_000_000
    source_time, binary_time = (new, old) if source_newer else (old, new)
    # Every source counts, so the manifest has to be dated too or it alone
    # would make the tree look newer than the binary.
    os.utime(manifest, (source_time, source_time))
    os.utime(source, (source_time, source_time))
    os.utime(binary, (binary_time, binary_time))
    return binary


def test_check_compiled_adapters_fails_when_the_binary_is_older_than_its_source(
    monkeypatch, tmp_path
):
    """A stale build runs, so every op added since it was compiled looks like an
    engine that cannot do the thing — a silently wrong result, not an outage."""
    binary = build_fake_adapter(tmp_path, source_newer=True)
    monkeypatch.setattr(runner_run, "compiled_adapter_binaries", lambda: {"rod": binary})
    assert runner_run.check_compiled_adapters(emit=False) is False


def test_check_compiled_adapters_accepts_a_binary_newer_than_its_source(monkeypatch, tmp_path):
    binary = build_fake_adapter(tmp_path, source_newer=False)
    monkeypatch.setattr(runner_run, "compiled_adapter_binaries", lambda: {"rod": binary})
    assert runner_run.check_compiled_adapters(emit=False) is True


# A `/` starts a regex literal (rather than division) when what precedes it
# cannot end an expression: either a punctuator that opens one, or a keyword.
# The keyword half is not optional — `return /not found|wasn't found/i` is real
# code in this repo, and misreading it as division lets the apostrophe open a
# bogus string literal that swallows the rest of the function.
REGEX_CAN_FOLLOW = set("(,=:[!&|?{};+-*%~^<>")
REGEX_CAN_FOLLOW_WORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete",
    "void", "case", "do", "else", "yield", "await",
}


def blank_literals(source: str) -> str:
    """Blank out comments and string/template/regex literals, preserving length.

    Paren counting has to run on code only. This file's own sources contain
    both `"http://..."` (a `//` that is not a comment) and `/wasn't found/i` (a
    quote inside a regex literal), so stripping one construct at a time with
    independent regexes mis-parses in either order — hence one pass that knows
    which state it is in. Offsets are preserved so spans stay sliceable against
    the original text.
    """
    out = list(source)
    idx = 0
    length = len(source)
    prev = ""
    word = ""       # identifier currently being read
    last_word = ""  # last complete identifier seen

    def blank(start: int, end: int) -> None:
        for pos in range(start, min(end, length)):
            if out[pos] != "\n":
                out[pos] = " "

    def starts_regex() -> bool:
        if prev == "" or prev in REGEX_CAN_FOLLOW:
            return True
        # `prev` is the tail of an identifier: only a keyword can precede a regex.
        return (prev.isalnum() or prev in "_$") and last_word in REGEX_CAN_FOLLOW_WORDS

    while idx < length:
        char = source[idx]
        nxt = source[idx + 1] if idx + 1 < length else ""
        if char == "/" and nxt == "/":
            end = source.find("\n", idx)
            end = length if end < 0 else end
            blank(idx, end)
            idx = end
            continue
        if char == "/" and nxt == "*":
            end = source.find("*/", idx + 2)
            end = length if end < 0 else end + 2
            blank(idx, end)
            idx = end
            continue
        if char in "\"'`" or (char == "/" and starts_regex()):
            closer = char
            pos = idx + 1
            while pos < length:
                if source[pos] == "\\":
                    pos += 2
                    continue
                if source[pos] == closer:
                    pos += 1
                    break
                if closer == "/" and source[pos] == "\n":
                    break  # unterminated: not a regex after all
                pos += 1
            blank(idx, pos)
            idx = pos
            continue
        if char.isalnum() or char in "_$":
            word += char
        else:
            if word:
                last_word = word
            word = ""
        if not char.isspace():
            prev = char
        idx += 1
    return "".join(out)


def race_spans(source: str):
    """Yield the source text of each `Promise.race(...)` call, parens balanced."""
    code = blank_literals(source)
    for match in re.finditer(r"Promise\.race\(", code):
        depth = 0
        start = match.end() - 1
        for idx in range(start, len(code)):
            char = code[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    yield source[start : idx + 1]
                    break


def test_race_spans_finds_nested_calls():
    span = next(race_spans("await Promise.race([f(g(1)), h(2)]);"))
    assert span == "([f(g(1)), h(2)])"


def test_race_spans_ignores_parens_inside_literals_and_comments():
    # An unbalanced paren in any of these would shift the depth count and make
    # the guard below silently inspect the wrong text.
    for noise in ('"budget (remaining"', "'oops ('", "`tpl (`", "/re (/", "/* b ( */", "// c (\n"):
        src = f"await Promise.race([a({noise}), b()]);"
        assert next(race_spans(src)) == f"([a({noise}), b()])", noise


def test_blank_literals_handles_this_repos_tricky_constructs():
    # A `//` inside a string is not a comment...
    assert "127.0.0.1" not in blank_literals('const u = "http://127.0.0.1"; race(')
    assert "race(" in blank_literals('const u = "http://127.0.0.1"; race(')
    # ...and a quote inside a regex literal does not open a string. This one is
    # load-bearing: cri_adapter.js really does `return /not found|wasn't .../i`,
    # and reading that `/` as division let the apostrophe swallow the next 20
    # lines — including, in principle, a Promise.race this file must inspect.
    assert "keepme" in blank_literals("return /wasn't found/i.test(m); keepme();")
    # A real division must still be treated as division, not as a regex opener.
    assert blank_literals("const half = total / 2; race(") == "const half = total / 2; race("


@pytest.mark.parametrize("path", js_sources(), ids=lambda p: p.name)
def test_blank_literals_leaves_balanced_code(path: pathlib.Path):
    # Backstop: any mis-parse in blank_literals shows up as unbalanced code and
    # fails loudly here, instead of silently hiding a Promise.race from the
    # guard below and letting a regression through.
    code = blank_literals(path.read_text(encoding="utf-8"))
    for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
        assert code.count(opener) == code.count(closer), (
            f"{path.name}: blank_literals() left {opener}{closer} unbalanced — it mis-parsed "
            "a literal or comment, so the Promise.race guard cannot be trusted on this file"
        )


@pytest.mark.parametrize("path", js_sources(), ids=lambda p: p.name)
def test_race_deadline_timers_do_not_hold_the_process_open(path: pathlib.Path):
    source = path.read_text(encoding="utf-8")
    for span in race_spans(source):
        if "setTimeout(" in span:
            assert ".unref()" in span, (
                f"{path.name}: a setTimeout inside Promise.race is not unref'd. The losing "
                "timer stays armed for its full duration and keeps the event loop alive "
                "after the result is emitted, so the runner kills the attempt as a timeout."
            )
        # `wait()` resolves through a ref'd timer; `waitUnref()` is its
        # process-exit-safe twin and is what a race deadline must use.
        assert not re.search(r"(?<![A-Za-z0-9_])wait\(", span), (
            f"{path.name}: a Promise.race deadline uses wait(); use waitUnref() so the "
            "losing timer cannot keep the process alive past its result."
        )
