"""TESTING.md §2 — pure helper functions.

Covers grade_inline / grade_inline_check (legacy strings plus every generic
kind added after TESTING.md was written), seed_for_attempt determinism,
is_unsupported_error, parse_engines, substitute_params / substitute_saved /
lookup_saved_path, find_free_port, and the write_json/append_jsonl/read_jsonl
disk invariants (§6).
"""
from __future__ import annotations

import io
import json
import pathlib
import socket

import pytest

from runner import run as runner_run
from _fakes import make_resolved, make_task_dict


# --- grade_inline: legacy string checks -------------------------------------


def _grade(checks, saved):
    task = make_resolved(task=make_task_dict(grader={"kind": "inline_assertions", "checks": checks}))
    return runner_run.grade_inline(task, saved)


def test_legacy_value_equals_3_pass_and_fail():
    good = _grade(["value_equals_3"], {"value": 3, "value__type": "number"})
    assert good["ok"] is True and good["failure"] is None
    bad = _grade(["value_equals_3"], {"value": 2, "value__type": "number"})
    assert bad["ok"] is False
    assert bad["failure"]["class"] == "cdp_semantic"


def test_legacy_result_type_number():
    assert _grade(["result_type_number"], {"value": 3, "value__type": "number"})["ok"] is True
    assert _grade(["result_type_number"], {"value": "3", "value__type": "string"})["ok"] is False


def test_unknown_legacy_string_check_fails():
    result = _grade(["value_equals_42"], {"value": 42})
    assert result["ok"] is False
    assert result["checks"][0]["evidence"] == "unknown inline assertion"


def test_one_failing_check_fails_the_grader():
    result = _grade(["value_equals_3", "result_type_number"], {"value": 3, "value__type": "string"})
    assert result["ok"] is False
    statuses = {check["name"]: check["status"] for check in result["checks"]}
    assert statuses == {"value_equals_3": "pass", "result_type_number": "fail"}


def test_empty_checks_list_fails():
    # Fixed: a grader with zero checks must not pass vacuously; a synthetic
    # `checks_declared` failure is injected.
    result = _grade([], {})
    assert result["ok"] is False
    assert result["checks"][0]["name"] == "checks_declared"


# --- grade_inline_check: generic kinds ---------------------------------------


def test_kind_value_equals():
    ok, _ = runner_run.grade_inline_check({"kind": "value_equals", "name": "value", "expected": 3}, {"value": 3})
    assert ok is True
    ok, _ = runner_run.grade_inline_check({"kind": "value_equals", "name": "value", "expected": 4}, {"value": 3})
    assert ok is False


def test_kind_value_type():
    saved = {"value__type": "number"}
    assert runner_run.grade_inline_check({"kind": "value_type", "name": "value", "expected": "number"}, saved)[0]
    assert not runner_run.grade_inline_check({"kind": "value_type", "name": "value", "expected": "string"}, saved)[0]


def test_kind_value_truthy():
    assert runner_run.grade_inline_check({"kind": "value_truthy", "name": "value"}, {"value": "yes"})[0]
    assert not runner_run.grade_inline_check({"kind": "value_truthy", "name": "value"}, {"value": 0})[0]
    assert not runner_run.grade_inline_check({"kind": "value_truthy", "name": "value"}, {})[0]


def test_kind_value_contains():
    saved = {"value": "hello world"}
    assert runner_run.grade_inline_check({"kind": "value_contains", "name": "value", "expected": "world"}, saved)[0]
    assert not runner_run.grade_inline_check({"kind": "value_contains", "name": "value", "expected": "mars"}, saved)[0]
    # non-string saved value never contains anything
    assert not runner_run.grade_inline_check({"kind": "value_contains", "name": "value", "expected": "1"}, {"value": 12})[0]


def test_kind_eval_exception_counting():
    none = {}
    two = {"__eval_exception_count__": 2}
    assert runner_run.grade_inline_check({"kind": "eval_no_exception"}, none)[0] is True
    assert runner_run.grade_inline_check({"kind": "eval_no_exception"}, two)[0] is False
    assert runner_run.grade_inline_check({"kind": "eval_has_exception"}, none)[0] is False
    assert runner_run.grade_inline_check({"kind": "eval_has_exception"}, two)[0] is True


def test_kind_no_error_always_passes():
    assert runner_run.grade_inline_check({"kind": "no_error"}, {})[0] is True


def test_unknown_generic_kind_fails():
    ok, evidence = runner_run.grade_inline_check({"kind": "value_matches_regex"}, {})
    assert ok is False
    assert "unknown inline assertion kind" in evidence


def test_generic_check_label_used_in_report():
    task = make_resolved(
        task=make_task_dict(
            grader={
                "kind": "inline_assertions",
                "checks": [{"kind": "value_equals", "name": "value", "expected": 1, "label": "one_is_one"}],
            }
        )
    )
    result = runner_run.grade_inline(task, {"value": 1})
    assert result["checks"][0]["name"] == "one_is_one"
    assert result["ok"] is True


# --- seed_for_attempt ---------------------------------------------------------


def test_seed_for_attempt_is_deterministic():
    task = make_resolved()
    a = runner_run.seed_for_attempt("base", task, 1)
    b = runner_run.seed_for_attempt("base", task, 1)
    assert a == b
    assert len(a) == 12
    int(a, 16)  # hex digest prefix


def test_seed_differs_by_attempt_and_task():
    task1 = make_resolved()
    task2 = make_resolved(task=make_task_dict(task_id="other_task"))
    assert runner_run.seed_for_attempt("base", task1, 1) != runner_run.seed_for_attempt("base", task1, 2)
    assert runner_run.seed_for_attempt("base", task1, 1) != runner_run.seed_for_attempt("base", task2, 1)


def test_seed_without_base_is_random_but_well_formed():
    task = make_resolved()
    a = runner_run.seed_for_attempt(None, task, 1)
    b = runner_run.seed_for_attempt(None, task, 1)
    assert len(a) == len(b) == 12
    assert a != b  # fresh entropy per call


# --- is_unsupported_error -----------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Runtime.evaluate: Method not found",
        "'IndexedDB.enable' is unsupported",
        "unknown method 'Fetch.enable'",
        "target NOT FOUND",
    ],
)
def test_unsupported_error_positive(message):
    assert runner_run.is_unsupported_error(Exception(message)) is True


@pytest.mark.parametrize("message", ["Invalid parameters", "Cannot navigate to invalid URL", "boom"])
def test_unsupported_error_negative(message):
    assert runner_run.is_unsupported_error(Exception(message)) is False


def test_chrome_style_wasnt_found_classified_unsupported():
    # Fixed: Chrome's real unknown-method error text "'X' wasn't found" maps to
    # engine_unsupported per TESTING.md §3.
    assert runner_run.is_unsupported_error(Exception("'Foo.bar' wasn't found")) is True


# --- parse_engines ------------------------------------------------------------


def test_parse_engines_orders_canonically():
    assert runner_run.parse_engines("lightpanda,chrome") == ["chrome", "lightpanda"]
    assert runner_run.parse_engines("obscura,moli,lightpanda,chrome") == [
        "chrome",
        "moli",
        "lightpanda",
        "obscura",
    ]


def test_parse_engines_dedupes():
    assert runner_run.parse_engines("chrome,chrome, chrome") == ["chrome"]


def test_parse_engines_unknown_raises():
    with pytest.raises(runner_run.BenchError, match="unknown engine"):
        runner_run.parse_engines("chrome,firefox")


# --- run digest helpers -------------------------------------------------------


def test_expected_result_rows_counts_selected_engine_matrix():
    tasks = [make_resolved(task=make_task_dict(task_id="t1")), make_resolved(task=make_task_dict(task_id="t2"))]
    assert runner_run.expected_result_rows(tasks, 3, ["chrome", "moli"]) == 12


def test_count_tasks_by_and_format_count_map_are_stable():
    tasks = [
        make_resolved(task=make_task_dict(layer="L2", subset_id="l2.web_platform", task_id="a")),
        make_resolved(task=make_task_dict(layer="L1", subset_id="l1.raw_cdp", task_id="b")),
        make_resolved(task=make_task_dict(layer="L2", subset_id="l2.web_platform", task_id="c")),
    ]
    assert runner_run.count_tasks_by(tasks, "layer") == {"L1": 1, "L2": 2}
    assert runner_run.format_count_map({"z": 1, "a": 2}) == "a=2, z=1"


def test_run_digest_reports_host_telemetry_off(tmp_path):
    task = make_resolved(task=make_task_dict())
    lines = runner_run.build_run_digest_lines(
        suite={"bench_id": "bench", "bench_version": "test"},
        manifest_path=runner_run.DEFAULT_MANIFEST,
        tasks=[task],
        selected_engines=["moli"],
        run_id="digest-test",
        run_dir=tmp_path / "run",
        k_runs=1,
        jobs=1,
        score_mode="independent",
        score_eligible=True,
        score_reasons=[],
        chrome_gate="off",
        fixture_base_url=None,
        resource_profile="off",
        host_telemetry_enabled=False,
    )
    resources = next(line for line in lines if line.startswith("resources"))
    assert resources == "resources   host=off; engine_profile=off"


def test_summary_and_scorecard_expose_failure_origin(tmp_path):
    row = {
        "layer": "L1",
        "subset_id": "l1.raw_cdp",
        "task_id": "timeout-task",
        "engine": "moli",
        "attempt": 1,
        "status": "timeout",
        "score_included": True,
        "chrome_gate": {"required": False},
        "failure": {
            "class": "infra",
            "origin": "task_timeout",
            "detail": "timed out",
        },
    }
    manifest = {
        "run_id": "origin-test",
        "score_eligible": True,
        "enabled_subsets": ["l1.raw_cdp"],
        "engines": {},
        "resolved_tasks": [],
    }
    scores = runner_run.summarize_results(manifest, [row])
    assert scores["failure_origins"] == {"task_timeout": 1}

    runner_run.write_scorecard(tmp_path, manifest, [row], scores)
    scorecard = (tmp_path / "scorecard.md").read_text()
    assert "failure.origin" in scorecard
    assert "| task_timeout | 1 |" in scorecard


def test_l2_summary_reports_all_candidate_pairs():
    statuses = {"moli": "pass", "lightpanda": "fail", "obscura": "pass"}
    rows = [
        {
            "layer": "L2",
            "subset_id": "l2.web_platform",
            "task_id": "candidate-pair",
            "engine": engine,
            "attempt": 1,
            "status": status,
            "score_included": True,
            "chrome_gate": {"required": False},
            "failure": None,
        }
        for engine, status in statuses.items()
    ]
    scores = runner_run.summarize_results(
        {"selected_engines": list(statuses)}, rows
    )
    pairs = scores["layers"]["L2"]["candidate_pairwise"]
    assert pairs["moli__lightpanda"]["left_only"] == 1
    assert pairs["moli__obscura"]["both_pass"] == 1
    assert pairs["lightpanda__obscura"]["right_only"] == 1
    assert all(payload["missing"] == 0 for payload in pairs.values())


def test_run_reporter_color_modes():
    colored = io.StringIO()
    reporter = runner_run.RunReporter(1, progress=False, color_mode="always", stream=colored)
    reporter.write_digest(["Agent Browser Bench run", "-----------------------", "run_id      demo"])
    assert "\033[" in colored.getvalue()
    assert runner_run.strip_ansi(colored.getvalue()).startswith("Agent Browser Bench run")

    plain = io.StringIO()
    reporter = runner_run.RunReporter(1, progress=False, color_mode="never", stream=plain)
    reporter.write_digest(["Agent Browser Bench run"])
    assert "\033[" not in plain.getvalue()


def test_reserve_run_dir_uses_utc_stamp_without_explicit_id(tmp_path):
    stamp = runner_run.run_id_conflict_stamp()
    run_id, run_dir = runner_run.reserve_run_dir(tmp_path)
    assert run_id == stamp
    assert run_dir == tmp_path / stamp
    assert run_dir.is_dir()


def test_reserve_run_dir_refuses_existing_when_error_mode_requested(tmp_path):
    (tmp_path / "ov1r1").mkdir()
    with pytest.raises(runner_run.BenchError, match="refusing to overwrite"):
        runner_run.reserve_run_dir(tmp_path, "ov1r1", "error")


def test_reserve_run_dir_uses_explicit_id_verbatim(tmp_path):
    run_id, run_dir = runner_run.reserve_run_dir(tmp_path, "ov1r1")
    assert run_id == "ov1r1"
    assert run_dir == tmp_path / "ov1r1"
    assert run_dir.is_dir()


def test_reserve_run_dir_suffix_adds_counter_on_conflict(tmp_path):
    (tmp_path / "ov1r1").mkdir()
    run_id, run_dir = runner_run.reserve_run_dir(tmp_path, "ov1r1")
    assert run_id == "ov1r1_002"
    assert run_dir.is_dir()


def test_reserve_run_dir_suffix_adds_counter_without_explicit_id(tmp_path):
    stamp = runner_run.run_id_conflict_stamp()
    (tmp_path / stamp).mkdir()
    run_id, run_dir = runner_run.reserve_run_dir(tmp_path)
    assert run_id == f"{stamp}_002"
    assert run_dir.is_dir()


def test_compact_run_id_keeps_directory_name_bounded():
    run_id = runner_run.compact_run_id("x" * 200)
    assert len(run_id) == 64
    assert run_id.startswith("x")
    assert "_" in run_id


# --- substitute_params / substitute_saved / lookup_saved_path ------------------


def test_substitute_params_recurses_and_preserves_non_strings():
    value = {
        "expression": "fetch('{fixture_url}/x')",
        "count": 5,
        "nested": ["{fixture_url}", {"deep": "{fixture_url}"}],
        "untouched": None,
    }
    out = runner_run.substitute_params(value, {"{fixture_url}": "http://127.0.0.1:1"})
    assert out["expression"] == "fetch('http://127.0.0.1:1/x')"
    assert out["count"] == 5
    assert out["nested"] == ["http://127.0.0.1:1", {"deep": "http://127.0.0.1:1"}]
    assert out["untouched"] is None


def test_lookup_saved_path_prefers_raw_and_walks_dicts_and_lists():
    saved = {
        "value": "flattened",
        "value__raw": {"result": {"value": {"items": [10, 20, 30]}}},
        "plain": 7,
    }
    assert runner_run.lookup_saved_path(saved, "value.result.value.items.1") == 20
    assert runner_run.lookup_saved_path(saved, "plain") == 7
    assert runner_run.lookup_saved_path(saved, "value.result.missing") is None
    assert runner_run.lookup_saved_path(saved, "value.result.value.items.9") is None
    # non-digit segment against a list yields None
    assert runner_run.lookup_saved_path(saved, "value.result.value.items.first") is None
    assert runner_run.lookup_saved_path(saved, "ghost") is None


def test_substitute_saved_whole_string_placeholder_only():
    saved = {"value": 7, "value__raw": {"result": {"value": 7}}}
    params = {
        "typed": "{saved:value.result.value}",
        "partial": "prefix {saved:value} suffix",
        "list": ["{saved:value.result.value}"],
    }
    out = runner_run.substitute_saved(params, saved)
    assert out["typed"] == 7  # type-preserving substitution
    assert out["partial"] == "prefix {saved:value} suffix"  # only full-string placeholders match
    assert out["list"] == [7]


def test_substitute_saved_missing_path_becomes_none():
    assert runner_run.substitute_saved("{saved:nope.deep}", {}) is None


# --- find_free_port -------------------------------------------------------------


def test_find_free_port_returns_bindable_port():
    port = runner_run.find_free_port()
    assert isinstance(port, int) and 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))  # still free


def test_moli_default_serve_command_keeps_crawler_resource_policy():
    command = runner_run.serve_engine_launch_command(
        "moli", pathlib.Path("/tmp/moli"), 9333
    )
    assert command == [
        "/tmp/moli",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "9333",
    ]


def test_moli_all_resources_profile_enables_full_resource_fetch():
    command = runner_run.serve_engine_launch_command(
        "moli",
        pathlib.Path("/tmp/moli"),
        9333,
        "all_resources",
    )
    assert command == [
        "/tmp/moli",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "9333",
        "--resource",
    ]


def test_serve_command_keeps_engine_specific_flags_isolated():
    lightpanda = runner_run.serve_engine_launch_command(
        "lightpanda", pathlib.Path("/tmp/lightpanda"), 9334
    )
    obscura = runner_run.serve_engine_launch_command(
        "obscura", pathlib.Path("/tmp/obscura"), 9335, "all_resources"
    )
    assert "--resource" not in lightpanda
    assert "--resource" not in obscura
    assert obscura[-1] == "--allow-private-network"


def test_browser_manager_replaces_moli_when_task_profile_changes(
    tmp_path, monkeypatch
):
    binary = tmp_path / "moli"
    binary.touch()
    monkeypatch.setitem(runner_run.ENGINE_DEFS["moli"], "binary", binary)
    monkeypatch.setattr(runner_run, "port_is_open", lambda _port: True)

    manager = runner_run.BrowserManager()
    launched = []
    killed = []

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    def fake_launch(engine, launched_binary, port, launch_profile):
        browser = runner_run.BrowserProcess(
            engine=engine,
            port=port,
            process=Proc(1000 + len(launched)),
            version_info={},
            binary=launched_binary,
            serve_args=runner_run.engine_serve_args(engine, launch_profile),
        )
        launched.append((launch_profile, browser))
        manager.processes[engine] = browser
        return browser

    monkeypatch.setattr(manager, "_launch_on_port", fake_launch)
    monkeypatch.setattr(manager, "_kill_process", lambda proc: killed.append(proc))

    default = manager.launch("moli")
    assert manager.launch("moli") is default
    all_resources = manager.launch("moli", "all_resources")

    assert all_resources is not default
    assert [profile for profile, _browser in launched] == [
        "default",
        "all_resources",
    ]
    assert killed == [default.process]
    assert manager.processes == {"moli": all_resources}


# --- write_json / append_jsonl / read_jsonl (TESTING.md §6) ----------------------


def test_write_json_atomic_no_tmp_leftovers(tmp_path):
    path = tmp_path / "deep" / "payload.json"
    runner_run.write_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    leftovers = [p for p in path.parent.iterdir() if "tmp-" in p.name]
    assert leftovers == []


def test_append_and_read_jsonl_roundtrip(tmp_path):
    path = tmp_path / "rows.jsonl"
    runner_run.append_jsonl(path, {"n": 1})
    runner_run.append_jsonl(path, {"n": 2})
    assert runner_run.read_jsonl(path) == [{"n": 1}, {"n": 2}]


def test_read_jsonl_bad_row_reports_line_number(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"ok": 1}\n{broken\n', encoding="utf-8")
    with pytest.raises(runner_run.BenchError, match=r":2: invalid JSONL row"):
        runner_run.read_jsonl(path)


def test_read_jsonl_missing_file_returns_empty(tmp_path):
    assert runner_run.read_jsonl(tmp_path / "nope.jsonl") == []


# --- identity-located array assertions and body decoding ---------------------


AX_SAVED = {
    "ax": {
        "nodes": [
            {"role": {"value": "WebArea"}, "name": {"value": "page"}},
            {
                "role": {"value": "checkbox"},
                "name": {"value": "Analytics"},
                "properties": [{"name": "checked", "value": {"value": "true"}}],
            },
            {
                "role": {"value": "checkbox"},
                "name": {"value": "Beta UI"},
                "properties": [{"name": "checked", "value": {"value": "false"}}],
            },
        ]
    }
}


def _ax_check(name, expected_state):
    return {
        "kind": "array_match_contains",
        "name": "ax",
        "path": "nodes",
        "match": {"role": {"value": "checkbox"}, "name": {"value": name}},
        "contains_path": "properties",
        "expected": {"name": "checked", "value": {"value": expected_state}},
    }


def test_array_match_contains_locates_node_by_identity():
    ok, evidence = runner_run.grade_inline_check(_ax_check("Analytics", "true"), AX_SAVED)
    assert ok, evidence
    # Same nodes in a different order must still pass: identity, not position.
    reordered = {"ax": {"nodes": list(reversed(AX_SAVED["ax"]["nodes"]))}}
    ok, evidence = runner_run.grade_inline_check(_ax_check("Analytics", "true"), reordered)
    assert ok, evidence


def test_array_match_contains_fails_on_wrong_state_or_missing_node():
    ok, _ = runner_run.grade_inline_check(_ax_check("Analytics", "false"), AX_SAVED)
    assert not ok
    ok, evidence = runner_run.grade_inline_check(_ax_check("Publish", "true"), AX_SAVED)
    assert not ok
    assert "matched 0 entries" in evidence


def test_array_match_contains_requires_unambiguous_identity():
    duplicated = {"ax": {"nodes": [AX_SAVED["ax"]["nodes"][1]] * 2}}
    ok, evidence = runner_run.grade_inline_check(_ax_check("Analytics", "true"), duplicated)
    assert not ok
    assert "matched 2 entries" in evidence


def test_saved_body_text_equals_accepts_both_wire_representations():
    check = {
        "kind": "saved_body_text_equals",
        "name": "orig",
        "expected": "original-document-body-token-ORIG",
    }
    plain = {"orig": {"base64Encoded": False, "body": "original-document-body-token-ORIG"}}
    encoded = {
        "orig": {
            "base64Encoded": True,
            "body": "b3JpZ2luYWwtZG9jdW1lbnQtYm9keS10b2tlbi1PUklH",
        }
    }
    for saved in (plain, encoded):
        ok, evidence = runner_run.grade_inline_check(check, saved)
        assert ok, evidence


def test_saved_body_text_equals_rejects_wrong_content_and_bad_base64():
    check = {"kind": "saved_body_text_equals", "name": "orig", "expected": "right"}
    ok, _ = runner_run.grade_inline_check(
        check, {"orig": {"base64Encoded": False, "body": "wrong"}}
    )
    assert not ok
    ok, evidence = runner_run.grade_inline_check(
        check, {"orig": {"base64Encoded": True, "body": "!!!not-base64!!!"}}
    )
    assert not ok
    assert "decode" in evidence


def test_saved_body_text_equals_requires_string_expected():
    # PR #132 review: a missing `expected` must fail loudly, not compare
    # against the coerced string "None".
    ok, evidence = runner_run.grade_inline_check(
        {"kind": "saved_body_text_equals", "name": "orig"},
        {"orig": {"base64Encoded": False, "body": "None"}},
    )
    assert not ok
    assert "must be a string" in evidence
