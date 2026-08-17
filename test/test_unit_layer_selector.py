from __future__ import annotations

import pytest

from runner import run as runner_run
from _fakes import make_l2_task_dict, make_task_dict


def test_layer_selector_is_repeatable_for_all_commands():
    parser = runner_run.build_parser()
    for command in ("validate", "list", "run"):
        args = parser.parse_args([command, "--layer", "L1", "--layer", "L2"])
        assert args.layer == ["L1", "L2"]


def test_layer_union_and_cross_dimension_intersection(bench_factory):
    l1 = make_task_dict(features=["shared", "l1-only"], tags=["selected"])
    l2 = make_l2_task_dict(features=["shared", "l2-only"], tags=["selected"])
    manifest = bench_factory(l1_tasks=[l1], l2_tasks=[l2])

    _suite, both = runner_run.expand_tasks(manifest, requested_layers=["L1", "L2"])
    assert {task.layer for task in both} == {"L1", "L2"}

    _suite, selected = runner_run.expand_tasks(
        manifest,
        requested_subsets=["l1.raw_cdp"],
        requested_tasks=[l1["task_id"]],
        requested_features=["shared"],
        requested_tags=["selected"],
        requested_layers=["L1"],
    )
    assert [task.task_id for task in selected] == [l1["task_id"]]


def test_unknown_layer_and_empty_subset_intersection_are_readable(bench_factory):
    manifest = bench_factory(l1_tasks=[make_task_dict()], l2_tasks=[make_l2_task_dict()])

    with pytest.raises(runner_run.BenchError, match=r"unknown layer\(s\): L9; expected L1, L2"):
        runner_run.expand_tasks(manifest, requested_layers=["L9"])
    with pytest.raises(runner_run.BenchError, match="selection resolved to no subsets"):
        runner_run.expand_tasks(manifest, requested_subsets=["l1.raw_cdp"], requested_layers=["L2"])
    with pytest.raises(runner_run.BenchError, match="selector intersection resolved to no tasks"):
        runner_run.expand_tasks(manifest, requested_features=["l2-only"], requested_layers=["L1"])
    with pytest.raises(runner_run.BenchError, match=r"task\(s\) not found in layer\(s\) L1"):
        runner_run.expand_tasks(manifest, requested_tasks=[make_l2_task_dict()["task_id"]], requested_layers=["L1"])


def test_missing_enabled_flags_default_to_enabled(bench_factory):
    def remove_enabled(manifest):
        manifest["layers"][0].pop("enabled")
        manifest["layers"][0]["subsets"][0].pop("enabled", None)

    manifest = bench_factory(l1_tasks=[make_task_dict()], manifest_mut=remove_enabled)
    _suite, tasks = runner_run.expand_tasks(manifest, requested_layers=["L1"])
    assert len(tasks) == 1


def test_current_manifest_layer_counts():
    _suite, l1 = runner_run.expand_tasks(runner_run.DEFAULT_MANIFEST, requested_layers=["L1"])
    _suite, l2 = runner_run.expand_tasks(runner_run.DEFAULT_MANIFEST, requested_layers=["L2"])
    _suite, all_tasks = runner_run.expand_tasks(runner_run.DEFAULT_MANIFEST, requested_layers=["L1", "L2"])
    _suite, unfiltered = runner_run.expand_tasks(runner_run.DEFAULT_MANIFEST)

    # L1 carries the 10 tasks #124 moved down from L2, plus the 79 agent-side
    # driver primitives added here.
    assert (len({task.subset_id for task in l1}), len(l1)) == (15, 1740)
    assert (len({task.subset_id for task in l2}), len(l2)) == (3, 188)
    assert len(all_tasks) == 1928
    assert len(unfiltered) == 1928
    assert _suite["default_k_runs"] == runner_run.DEFAULT_K_RUNS == 1
    assert runner_run.expected_result_rows(l1, 3, ["chrome", "moli", "lightpanda"]) == 15660
