"""TESTING.md §2 — validate_manifest / validate_task schema rules.

One good sample plus one bad sample per rule, asserting the exact error
substring produced by runner.run. run.py has been extended since TESTING.md
was written; this also covers the newer rule: raw_cdp needs a non-empty
steps list.
"""
from __future__ import annotations

import pytest

from runner import run as runner_run
from _fakes import make_task_dict, make_l2_task_dict


def errors_for(bench_factory, **kw):
    manifest_path = bench_factory(**kw)
    _suite, _tasks, errors = runner_run.validate_manifest(manifest_path)
    return errors


def assert_error(errors, substring):
    assert any(substring in error for error in errors), f"expected {substring!r} in {errors}"


# --- good samples -----------------------------------------------------------


def test_good_manifest_and_tasks_have_no_errors(bench_factory):
    errors = errors_for(bench_factory, l1_tasks=[make_task_dict()], l2_tasks=[make_l2_task_dict()])
    assert errors == []


# --- per-rule bad samples ---------------------------------------------------


def test_missing_required_field_grader(bench_factory):
    task = make_task_dict()
    del task["grader"]
    task_resolvedless = task
    errors = errors_for(bench_factory, l1_tasks=[task_resolvedless])
    assert_error(errors, "missing required field `grader`")


def test_inline_grader_requires_verifiable_checks(bench_factory):
    task = make_task_dict(grader={"kind": "inline_assertions", "checks": []})
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "a return envelope alone is not verifiable")


def test_layer_mismatch(bench_factory):
    errors = errors_for(bench_factory, l1_tasks=[make_task_dict(layer="L2")])
    assert_error(errors, "does not match subset layer")


def test_subset_id_mismatch(bench_factory):
    errors = errors_for(bench_factory, l1_tasks=[make_task_dict(subset_id="l1.other")])
    assert_error(errors, "does not match `l1.raw_cdp`")


def test_score_lane_is_rejected(bench_factory):
    errors = errors_for(bench_factory, l1_tasks=[make_task_dict(score_lane="negative")])
    assert_error(errors, "obsolete field `score_lane` is not allowed")


@pytest.mark.parametrize(
    "field",
    ["source_reference", "migration_notes", "expected_status_claim"],
)
def test_historical_task_metadata_is_rejected(bench_factory, field):
    errors = errors_for(bench_factory, l1_tasks=[make_task_dict(**{field: "old"})])
    assert_error(errors, f"obsolete field `{field}` is not allowed")


def test_manifest_status_and_score_policy_are_rejected(bench_factory):
    def mutate(manifest):
        manifest["layers"][0]["score_policy"] = "gate_and_diagnostic"
        manifest["layers"][0]["subsets"][0]["status"] = "main"

    errors = errors_for(bench_factory, l1_tasks=[make_task_dict()], manifest_mut=mutate)
    assert_error(errors, "obsolete field `score_policy` is not allowed")
    assert_error(errors, "uses obsolete field `status`")


def test_features_must_be_string_list(bench_factory):
    errors = errors_for(bench_factory, l1_tasks=[make_task_dict(features=[1, 2])])
    assert_error(errors, "features must be a string list")


def test_description_must_be_non_empty_string(bench_factory):
    errors = errors_for(bench_factory, l1_tasks=[make_task_dict(description="")])
    assert_error(errors, "description must be a non-empty string")


def test_tags_must_be_string_list(bench_factory):
    errors = errors_for(bench_factory, l1_tasks=[make_task_dict(tags=["smoke", 1])])
    assert_error(errors, "tags must be a string list")


def test_unsupported_driver_kind(bench_factory):
    task = make_task_dict(driver={"kind": "puppeteer_cdp"})
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "unsupported driver.kind `puppeteer_cdp`")


def test_driver_kind_must_match_subset_driver(bench_factory):
    # node_cdp_probe is a valid kind, but the l1.raw_cdp subset pins raw_cdp.
    task = make_task_dict(driver={"kind": "node_cdp_probe", "script": "runner/scripts/storage_indexeddb_inventory_001.js"})
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "does not match subset driver `raw_cdp`")


def test_node_cdp_probe_requires_script_field(bench_factory):
    task = make_l2_task_dict(driver={"kind": "node_cdp_probe"})
    errors = errors_for(bench_factory, l2_tasks=[task])
    assert_error(errors, "node_cdp_probe task must set driver.script")


def test_node_cdp_probe_script_not_found(bench_factory):
    task = make_l2_task_dict(driver={"kind": "node_cdp_probe", "script": "runner/scripts/definitely_missing_stub.js"})
    errors = errors_for(bench_factory, l2_tasks=[task])
    assert_error(errors, "driver.script not found")


def test_raw_cdp_requires_non_empty_steps(bench_factory):
    task = make_task_dict(driver={"kind": "raw_cdp", "steps": []})
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "non-empty driver.steps")


def test_raw_cdp_missing_steps_key(bench_factory):
    task = make_task_dict(driver={"kind": "raw_cdp"})
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "non-empty driver.steps")


@pytest.mark.parametrize(
    "host_path",
    [
        "/home/dev/bench_checkout/fixtures/upload.txt",
        "/tmp/shared-downloads",
        "file:///home/dev/fixtures/upload.txt",
        "file:///C:/Users/dev/fixtures/upload.txt",
        "file://server/share/upload.txt",
        r"C:\Users\dev\bench_checkout\fixtures\upload.txt",
        r"\\server\share\upload.txt",
        r"\Users\dev\fixtures\upload.txt",
        "{artifact_dir}/../../shared/upload.txt",
        "{fixture_path}/../secret.txt",
    ],
)
def test_host_absolute_paths_in_raw_file_fields_are_rejected(
    bench_factory, host_path
):
    task = make_task_dict(
        task_id="absolute_raw_file_path",
        driver={
            "kind": "raw_cdp",
            "steps": [
                {
                    "method": "DOM.setFileInputFiles",
                    "params": {"files": [host_path]},
                }
            ],
        },
    )
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "contains host absolute path")


@pytest.mark.parametrize(
    "host_url",
    [
        "file:///home/dev/fixture.html",
        "file:///C:/Users/dev/fixture.html",
        "file://server/share/fixture.html",
        r"C:\Users\dev\fixture.html",
        r"\\server\share\fixture.html",
        r"\Users\dev\fixture.html",
    ],
)
def test_host_paths_are_rejected_in_executable_url_fields(
    bench_factory, host_url
):
    task = make_task_dict(
        task_id="absolute_url",
        driver={
            "kind": "raw_cdp",
            "steps": [
                {
                    "method": "Page.navigate",
                    "params": {"url": host_url},
                }
            ],
        },
    )
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "contains host absolute path")


@pytest.mark.parametrize(
    "method",
    ["Browser.setDownloadBehavior", "Page.setDownloadBehavior"],
)
def test_host_paths_are_rejected_in_download_behavior(
    bench_factory, method
):
    task = make_task_dict(
        task_id=f"absolute_download_path_{method.split('.')[0].lower()}",
        driver={
            "kind": "raw_cdp",
            "steps": [
                {
                    "method": method,
                    "params": {
                        "behavior": "allow",
                        "downloadPath": "/tmp/shared-downloads",
                    },
                }
            ],
        },
    )
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "contains host absolute path")


@pytest.mark.parametrize(
    ("method", "field", "value"),
    [
        (
            "Browser.setDownloadBehavior",
            "downloadPath",
            "{fixture_path}",
        ),
        (
            "Page.setDownloadBehavior",
            "downloadPath",
            "{fixture_path}/downloads",
        ),
        (
            "Browser.setDownloadBehavior",
            "downloadPath",
            "{artifact_dir}-sibling",
        ),
        (
            "Browser.setDownloadBehavior",
            "downloadPath",
            "prefix{artifact_dir}",
        ),
        (
            "Browser.setDownloadBehavior",
            "downloadPath",
            r"{artifact_dir}\sibling",
        ),
        (
            "Browser.setDownloadBehavior",
            "downloadPath",
            "shared-downloads",
        ),
        (
            "DOM.setFileInputFiles",
            "files",
            ["{fixture_path}evil/secret.txt"],
        ),
        (
            "DOM.setFileInputFiles",
            "files",
            ["fixtures/v0_2/t2b/upload_sample.txt"],
        ),
    ],
)
def test_runner_path_tokens_require_leading_boundaries_and_field_authority(
    bench_factory, method, field, value
):
    params = {field: value}
    if method.endswith("setDownloadBehavior"):
        params["behavior"] = "allow"
    task = make_task_dict(
        task_id="unsafe_runner_path_token",
        driver={
            "kind": "raw_cdp",
            "steps": [
                {
                    "method": method,
                    "params": params,
                }
            ],
        },
    )
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "unsafe traversal")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", "probe --out=/tmp/shared-output.json"),
        ("command", r"C:\Users\dev\probe.exe --mode test"),
        ("command", r'probe --out="C:\Users\dev\shared-output.json"'),
        ("args", ["--out=/tmp/shared-output.json"]),
        ("args", ["--output", "/tmp/shared-output.json"]),
    ],
)
def test_host_paths_are_rejected_in_command_path_flags(
    bench_factory, field, value
):
    task = make_task_dict(
        driver={
            "kind": "raw_cdp",
            field: value,
            "steps": [{"method": "Runtime.enable"}],
        }
    )
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "contains host absolute path")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", "probe --output"),
        ("args", ["probe", "--out"]),
    ],
)
def test_command_path_flags_require_a_following_argument(
    bench_factory, field, value
):
    task = make_task_dict(
        driver={
            "kind": "raw_cdp",
            field: value,
            "steps": [{"method": "Runtime.enable"}],
        }
    )
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "path flag that has no following argument")


def test_host_path_validation_allows_web_paths_json_paths_and_prose(bench_factory):
    task = make_task_dict(
        description="An example host path /home/dev/output.txt is metadata only.",
        scene={"kind": "self_hosted_fixture", "url": "/home/dashboard"},
        driver={
            "kind": "raw_cdp",
            "steps": [
                {
                    "method": "Page.navigate",
                    "params": {"url": "/run/report"},
                },
                {
                    "method": "Page.navigate",
                    "params": {"url": "/track"},
                },
                {
                    "method": "Page.navigate",
                    "params": {"url": "/api/items"},
                },
                {
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": (
                            r"const example = 'C:\Users\dev\fixture.html';"
                            "\nlocation.pathname"
                        ),
                    },
                    "save_as": "pathname",
                },
                {
                    "method": "Network.deleteCookies",
                    "params": {
                        "name": "session",
                        "path": "/",
                    },
                },
                {
                    "method": "DOM.setFileInputFiles",
                    "params": {
                        "files": ["{fixture_path}/v0_2/t2b/upload_sample.txt"],
                    },
                },
                {
                    "method": "Browser.setDownloadBehavior",
                    "params": {
                        "behavior": "allow",
                        "downloadPath": "{artifact_dir}/downloads",
                    },
                },
            ],
        },
        grader={
            "kind": "inline_assertions",
            "checks": [
                {
                    "kind": "saved_path_contains",
                    "name": "navigation",
                    "path": "/request/url",
                    "expected": "/api/items",
                },
                {
                    "kind": "value_equals",
                    "name": "metadata_path",
                    "expected": "/home/dev/checkout/output.txt",
                },
            ],
        },
    )
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert not any("host absolute path" in error for error in errors)


def test_raw_cdp_screenshot_and_pdf_methods_are_outside_target(bench_factory):
    for method in ("Page.captureScreenshot", "Page.printToPDF"):
        task = make_task_dict(driver={"kind": "raw_cdp", "steps": [{"method": method}]})
        errors = errors_for(bench_factory, l1_tasks=[task])
        assert_error(errors, f"method `{method}` is outside the benchmark target")


def test_screenshot_and_pdf_features_are_outside_target(bench_factory):
    for feature in ("cdp.page.capture_screenshot", "fw.playwright.page.pdf", "tool.agent_browser.screenshot"):
        task = make_task_dict(features=[feature])
        errors = errors_for(bench_factory, l1_tasks=[task])
        assert_error(errors, f"feature `{feature}` is outside the benchmark target")


def test_unsupported_scene_kind(bench_factory):
    task = make_task_dict(scene={"kind": "remote_url"})
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "unsupported scene.kind `remote_url`")


def test_fixture_url_must_be_absolute_fixture_path(bench_factory):
    task = make_l2_task_dict(scene={"kind": "self_hosted_fixture", "url": "http://example.com/x"})
    errors = errors_for(bench_factory, l2_tasks=[task])
    assert_error(errors, "must be an absolute fixture path")


def test_unsupported_grader_kind(bench_factory):
    task = make_task_dict(grader={"kind": "magic_judge"})
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "unsupported grader.kind `magic_judge`")


def test_server_side_endpoint_must_be_absolute(bench_factory):
    task = make_l2_task_dict(grader={"kind": "server_side", "endpoint": "http://x/__grade__/a"})
    errors = errors_for(bench_factory, l2_tasks=[task])
    assert_error(errors, "grader.endpoint must be an absolute fixture path")


def test_unsupported_artifact_profile(bench_factory):
    task = make_task_dict(artifact_profile="l9_custom")
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "unsupported artifact_profile `l9_custom`")


def test_supported_task_launch_profiles(bench_factory):
    for launch_profile in ("default", "all_resources"):
        task = make_task_dict(launch_profile=launch_profile)
        assert errors_for(bench_factory, l1_tasks=[task]) == []


def test_unsupported_task_launch_profile(bench_factory):
    task = make_task_dict(launch_profile="fetch_everything_forever")
    errors = errors_for(bench_factory, l1_tasks=[task])
    assert_error(errors, "unsupported launch_profile `fetch_everything_forever`")


def test_l2_main_accepts_off_chrome_gate(bench_factory):
    task = make_l2_task_dict(chrome_gate="off")
    errors = errors_for(bench_factory, l2_tasks=[task])
    assert errors == []


def test_subset_gate_must_be_valid_when_present(bench_factory):
    def set_bad_gate(manifest):
        manifest["layers"][0]["subsets"][0]["chrome_gate"] = "sometimes"

    errors = errors_for(bench_factory, l1_tasks=[make_task_dict()], manifest_mut=set_bad_gate)
    assert_error(errors, "must resolve to a valid Chrome baseline policy")
    assert_error(errors, "invalid chrome_gate")


# --- manifest-level rules ---------------------------------------------------


def test_fallback_allowed_must_be_false(bench_factory):
    def set_fallback(manifest):
        manifest["fallback_allowed"] = True

    errors = errors_for(bench_factory, l1_tasks=[make_task_dict()], manifest_mut=set_fallback)
    assert_error(errors, "fallback_allowed must be false")


def test_missing_manifest_field(bench_factory):
    def drop_site(manifest):
        del manifest["site"]

    errors = errors_for(bench_factory, l1_tasks=[make_task_dict()], manifest_mut=drop_site)
    assert_error(errors, "missing required field `site`")


def test_empty_enabled_subset_without_allow_empty(bench_factory):
    def no_allow_empty(manifest):
        manifest["layers"][0]["subsets"][0]["allow_empty"] = False

    errors = errors_for(bench_factory, l1_tasks=[], manifest_mut=no_allow_empty)
    assert_error(errors, "expands to no tasks")


def test_unknown_requested_subset(bench_factory):
    manifest_path = bench_factory(l1_tasks=[make_task_dict()])
    _suite, _tasks, errors = runner_run.validate_manifest(manifest_path, ["l9.nope"], None)
    assert_error(errors, "unknown subset(s): l9.nope")


def test_requested_task_not_found(bench_factory):
    manifest_path = bench_factory(l1_tasks=[make_task_dict()])
    _suite, _tasks, errors = runner_run.validate_manifest(manifest_path, None, ["ghost_task_999"])
    assert_error(errors, "task(s) not found: ghost_task_999")


def test_manifest_not_found_reports_error(tmp_path):
    _suite, _tasks, errors = runner_run.validate_manifest(tmp_path / "missing.json")
    assert_error(errors, "manifest not found")


def test_expected_answer_grading_requires_registry_entry(bench_factory):
    task = make_l2_task_dict(
        task_id="unit_missing_expected_answer_entry",
        grader={"kind": "server_side", "endpoint": "/__grade__/expected_answer"},
    )
    errors = errors_for(bench_factory, l2_tasks=[task])
    assert_error(errors, "expected-answer registry has no entry")
