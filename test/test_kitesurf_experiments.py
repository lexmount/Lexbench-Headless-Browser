from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from runner import run as bench
from tools import kitesurf_experiments as experiments


def test_manifest_is_valid_and_kitesurf_stays_out_of_formal_engines() -> None:
    manifest = experiments.load_manifest()
    experiments.validate_manifest(manifest)

    assert manifest["formal_score_eligible"] is False
    assert manifest["canonical_branch"] == "kitesurf-eval"
    assert "kitesurf" not in bench.ENGINE_DEFS
    for recipe in manifest["recipes"]:
        report = recipe.get("report")
        if report:
            assert (experiments.REPO_ROOT / report).is_file()


def test_consolidated_branch_descends_from_recorded_main() -> None:
    manifest = experiments.load_manifest()
    completed = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            manifest["base_main_at_consolidation"],
            "HEAD",
        ],
        cwd=experiments.REPO_ROOT,
        check=False,
    )

    assert completed.returncode == 0


def test_static_and_orthogonal_recipes_keep_reproduction_gates() -> None:
    manifest = experiments.load_manifest()
    dynamic_manifest = (
        experiments.REPO_ROOT
        / manifest["defaults"]["dynamic_fixture_manifest"]
    )
    assert dynamic_manifest.is_file()
    assert json.loads(dynamic_manifest.read_text(encoding="utf-8"))["schema"] == (
        "experimental.kitesurf_dynamic_fixture.v1"
    )
    for recipe_id in (
        "pages_direct",
        "orthogonal_headline_20260808",
        "orthogonal_promise_20260808",
        "orthogonal_current_full",
    ):
        command = experiments.recipe_by_id(manifest, recipe_id)["command"]
        assert "--fixture-manifest" in command
        assert "{{static_fixture_manifest}}" in command

    for recipe_id in (
        "orthogonal_headline_20260808",
        "orthogonal_promise_20260808",
        "orthogonal_current_full",
    ):
        command = experiments.recipe_by_id(manifest, recipe_id)["command"]
        assert command.count("--expect-product") == 3
        assert command.count("--expect-protocol-version") == 3
        assert command.count("--expect-revision") == 3

    dynamic_recipes = (
        "raw_full",
        "raw_failure_rerun",
        "driver_gate",
        "driver_broad",
        "l2_semantic_sample",
    )
    for recipe_id in dynamic_recipes:
        command = experiments.recipe_by_id(manifest, recipe_id)["command"]
        assert "{{dynamic_fixture_manifest}}" in command
        assert "--fixture-manifest" in command


def test_every_kitesurf_lane_pins_the_manifest_identity() -> None:
    manifest = experiments.load_manifest()
    identity_placeholders = {
        "{{kitesurf_product}}",
        "{{kitesurf_protocol_version}}",
        "{{kitesurf_revision}}",
    }
    for recipe in manifest["recipes"]:
        command = recipe["command"]
        has_kitesurf_endpoint = any(
            "{{kitesurf_endpoint}}" in str(part) for part in command
        )
        if recipe["category"] == "orchestration":
            assert has_kitesurf_endpoint, recipe["id"]
        if not has_kitesurf_endpoint and recipe["category"] != "orchestration":
            continue
        assert all(
            any(placeholder in str(part) for part in command)
            for placeholder in identity_placeholders
        ), recipe["id"]

    for recipe_id in (
        "raw_full",
        "raw_failure_rerun",
        "driver_gate",
        "driver_broad",
        "l2_semantic_sample",
    ):
        command = experiments.recipe_by_id(manifest, recipe_id)["command"]
        assert command.count("--expect-product") == 1
        assert command.count("--expect-protocol-version") == 1
        assert command.count("--expect-revision") == 1


def test_manifest_validator_rejects_a_self_pinning_kitesurf_recipe() -> None:
    manifest = json.loads(json.dumps(experiments.load_manifest()))
    recipe = experiments.recipe_by_id(manifest, "raw_full")
    recipe["command"] = [
        part
        for part in recipe["command"]
        if part != "{{kitesurf_revision}}"
    ]

    with pytest.raises(experiments.RecipeError, match="omits Kitesurf identity"):
        experiments.validate_manifest(manifest)


def test_manifest_validator_rejects_orchestration_without_endpoint() -> None:
    # No orchestration-category recipe ships on this branch (the wait_pid
    # campaign pipelines stayed private), so synthesize one to prove the
    # validator still enforces the endpoint rule for the category.
    manifest = json.loads(json.dumps(experiments.load_manifest()))
    recipe = experiments.recipe_by_id(manifest, "raw_full")
    recipe["category"] = "orchestration"
    recipe["command"] = [
        part for part in recipe["command"] if part != "{{kitesurf_endpoint}}"
    ]

    with pytest.raises(experiments.RecipeError, match="omits Kitesurf endpoint"):
        experiments.validate_manifest(manifest)


def test_manifest_validator_rejects_unpinned_dynamic_fixture() -> None:
    manifest = json.loads(json.dumps(experiments.load_manifest()))
    recipe = experiments.recipe_by_id(manifest, "raw_full")
    recipe["command"] = [
        part
        for part in recipe["command"]
        if part != "{{dynamic_fixture_manifest}}"
    ]

    with pytest.raises(
        experiments.RecipeError,
        match="omits dynamic fixture manifest",
    ):
        experiments.validate_manifest(manifest)


def test_manifest_validator_rejects_external_dynamic_fixture_default() -> None:
    manifest = json.loads(json.dumps(experiments.load_manifest()))
    manifest["defaults"]["dynamic_fixture_manifest"] = "/tmp/uncommitted.json"

    with pytest.raises(
        experiments.RecipeError,
        match="must be repository-relative",
    ):
        experiments.validate_manifest(manifest)


def test_failure_rerun_recipe_expands_stable_task_selection(tmp_path) -> None:
    source = tmp_path / "results.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps({"task_id": "pass-task", "status": "pass"}),
                json.dumps({"task_id": "fail-a", "status": "fail"}),
                json.dumps({"task_id": "fail-b", "status": "fail"}),
                json.dumps({"task_id": "fail-a", "status": "fail"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "summary.json").write_text(
        json.dumps({"scope": {"completed_tasks": 4}}) + "\n",
        encoding="utf-8",
    )
    manifest = experiments.load_manifest()
    recipe = experiments.recipe_by_id(manifest, "raw_failure_rerun")

    command = experiments.render_command(
        manifest,
        recipe,
        {
            "source_results": str(source),
            "fixture_base_url": "https://fixtures.example",
            "output": "runs/new-rerun",
        },
    )

    assert command.count("--task") == 2
    assert command[command.index("--task") + 1] == "fail-a"
    assert "fail-b" in command
    assert "pass-task" not in command


def test_failure_rerun_rejects_cleanly_truncated_results(tmp_path) -> None:
    source = tmp_path / "results.jsonl"
    source.write_text(
        json.dumps({"task_id": "fail-a", "status": "fail"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "summary.json").write_text(
        json.dumps({"scope": {"completed_tasks": 2}}) + "\n",
        encoding="utf-8",
    )
    manifest = experiments.load_manifest()
    recipe = experiments.recipe_by_id(manifest, "raw_failure_rerun")

    with pytest.raises(experiments.RecipeError, match="results are incomplete"):
        experiments.render_command(
            manifest,
            recipe,
            {
                "source_results": str(source),
                "fixture_base_url": "https://fixtures.example",
                "output": "runs/new-rerun",
            },
        )


def test_failure_rerun_requires_sibling_summary(tmp_path) -> None:
    source = tmp_path / "results.jsonl"
    source.write_text(
        json.dumps({"task_id": "fail-a", "status": "fail"}) + "\n",
        encoding="utf-8",
    )
    manifest = experiments.load_manifest()
    recipe = experiments.recipe_by_id(manifest, "raw_failure_rerun")

    with pytest.raises(experiments.RecipeError, match="summary is not readable JSON"):
        experiments.render_command(
            manifest,
            recipe,
            {
                "source_results": str(source),
                "fixture_base_url": "https://fixtures.example",
                "output": "runs/new-rerun",
            },
        )


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (["not", "an", "object"], "row is not an object"),
        ({"task_id": "fail-a"}, "row has no status"),
        ({"status": "fail"}, "row has no task_id"),
    ],
)
def test_failure_rerun_rejects_malformed_complete_rows(
    tmp_path,
    row,
    message,
) -> None:
    source = tmp_path / "results.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    (tmp_path / "summary.json").write_text(
        json.dumps({"scope": {"completed_tasks": 1}}) + "\n",
        encoding="utf-8",
    )
    manifest = experiments.load_manifest()
    recipe = experiments.recipe_by_id(manifest, "raw_failure_rerun")

    with pytest.raises(experiments.RecipeError, match=message):
        experiments.render_command(
            manifest,
            recipe,
            {
                "source_results": str(source),
                "fixture_base_url": "https://fixtures.example",
                "output": "runs/new-rerun",
            },
        )
