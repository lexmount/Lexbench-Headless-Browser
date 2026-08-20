"""The two version axes: the dataset (`bench_version`) and the harness.

`bench_version` in manifest.json identifies the dataset — which tasks exist and
which graders and fixtures they run against. The harness version in
runner/version.py identifies the code that runs them. They are bumped for
different reasons, so both shapes are enforced here, and the harness version is
held to a single source of truth across pyproject.toml and package.json.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re

import pytest

from runner import run as runner_run
from runner.version import HARNESS_VERSION

REPO_ROOT = pathlib.Path(runner_run.REPO_ROOT)
SEMVER = re.compile(r"\d+\.\d+\.\d+")


def read_json(name: str) -> dict:
    return json.loads((REPO_ROOT / name).read_text(encoding="utf-8"))


def test_dataset_version_is_semver():
    assert SEMVER.fullmatch(read_json("manifest.json")["bench_version"])


def test_manifest_carries_no_second_version_label():
    # The fixture tree is part of what bench_version identifies, and a run
    # records its digest under runner.fixtures. A separate site_version would
    # be a second label to forget.
    assert "site_version" not in read_json("manifest.json")["site"]


def test_harness_version_is_semver():
    assert SEMVER.fullmatch(HARNESS_VERSION)


def test_npm_package_agrees_on_the_harness_version():
    assert read_json("package.json")["version"] == HARNESS_VERSION


def test_pyproject_reads_the_harness_version_from_one_place():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert 'attr = "runner.version.HARNESS_VERSION"' in text


@pytest.mark.parametrize("bad", ["2026.08.17-v0_4.2", "0.4", "v0.4.2", "0.4.2-rc1", ""])
def test_validate_rejects_a_non_semver_dataset_version(bench_factory, bad):
    def mutate(manifest):
        manifest["bench_version"] = bad

    manifest_path = bench_factory(manifest_mut=mutate)
    _suite, _tasks, errors = runner_run.validate_manifest(manifest_path)
    assert any("bench_version must be MAJOR.MINOR.PATCH" in error for error in errors)


def test_run_manifest_records_both_axes():
    manifest_path = REPO_ROOT / "manifest.json"
    suite, tasks, errors = runner_run.validate_manifest(
        manifest_path, requested_subsets=["l1.raw_cdp"]
    )
    assert errors == []
    args = argparse.Namespace(
        chrome_gate="off", score_mode="independent", jobs=1, k=1, seed="unit"
    )
    payload = runner_run.run_manifest_payload(
        args, suite, manifest_path, tasks[:1], ["moli"], "unit_versioning", True, [], None
    )
    assert payload["bench_version"] == suite["bench_version"]
    assert payload["harness_version"] == HARNESS_VERSION
    assert "site_version" not in payload["site"]
