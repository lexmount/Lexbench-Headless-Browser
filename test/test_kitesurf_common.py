from __future__ import annotations

import hashlib

from tools import kitesurf_common as common


def test_source_provenance_captures_dirty_tree_and_main_lineage(monkeypatch) -> None:
    head = "a" * 40
    main = "b" * 40
    tracked_diff = b"binary tracked diff"
    untracked_state = b"new-file.txt\0file\0new contents\0"

    monkeypatch.setattr(common, "source_commit", lambda: head)
    monkeypatch.setattr(
        common,
        "_git_bytes",
        lambda *args: (
            b" M runner/run.py\n?? new-file.txt\n"
            if args[0] == "status"
            else tracked_diff
        ),
    )
    monkeypatch.setattr(
        common,
        "_git_text",
        lambda *args: {
            ("rev-parse", "--verify", "origin/main"): main,
            ("merge-base", "HEAD", "origin/main"): main,
            ("rev-parse", "HEAD^{tree}"): "c" * 40,
            ("branch", "--show-current"): "experiment/kitesurf-evaluation",
        }.get(args),
    )
    monkeypatch.setattr(
        common,
        "_untracked_state",
        lambda: (["new-file.txt"], untracked_state),
    )

    provenance = common.capture_source_provenance(
        common.REPO_ROOT / "tools/kitesurf_common.py",
        runtime_executables=(
            common.REPO_ROOT / "tools/kitesurf_common.py",
        ),
    )

    combined = hashlib.sha256()
    combined.update(tracked_diff)
    combined.update(b"\0untracked\0")
    combined.update(untracked_state)
    assert provenance["head"] == head
    assert provenance["origin_main"] == main
    assert provenance["contains_origin_main"] is True
    assert provenance["dirty"] is True
    assert provenance["status_porcelain"][0] == " M runner/run.py"
    assert provenance["tracked_diff_sha256"] == hashlib.sha256(
        tracked_diff
    ).hexdigest()
    assert provenance["worktree_state_sha256"] == combined.hexdigest()
    assert provenance["source_files_sha256"]["tools/kitesurf_common.py"]
    executable = provenance["runtime_executables"][0]
    assert executable["invoked_path"].endswith("/tools/kitesurf_common.py")
    assert executable["resolved_path"].endswith("/tools/kitesurf_common.py")
    assert executable["sha256"] == hashlib.sha256(
        (common.REPO_ROOT / "tools/kitesurf_common.py").read_bytes()
    ).hexdigest()
