from __future__ import annotations

import hashlib
import json

import pytest

from tools import kitesurf_static_fixture as fixture


def write_manifest(tmp_path, files):
    path = tmp_path / "fixture.json"
    path.write_text(
        json.dumps(
            {
                "schema": fixture.MANIFEST_SCHEMA,
                "deployment_base_url": "https://fixtures.example/static",
                "source": {
                    "repository": "https://github.com/example/fixtures",
                    "commit": "a" * 40,
                },
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_verifier_confirms_every_manifest_file_and_writes_report(
    tmp_path,
) -> None:
    bodies = {
        "index.html": b"<h1>fixture</h1>\n",
        "v1/data.json": b'{"value":42}\n',
    }
    manifest = write_manifest(
        tmp_path,
        [
            {
                "path": path,
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            for path, body in bodies.items()
        ],
    )
    requested = []

    def fetch(url, expected_size, timeout_s):
        requested.append((url, expected_size, timeout_s))
        path = url.removeprefix("https://fixtures.example/static/")
        return bodies[path], url

    report_path = tmp_path / "verification.json"
    report = fixture.verify_fixture(
        "https://fixtures.example/static/",
        manifest,
        report_path=report_path,
        timeout_s=2.0,
        fetch=fetch,
    )

    assert report["verified"] is True
    assert report["verified_file_count"] == 2
    assert [row["path"] for row in report["files"]] == list(bodies)
    assert all(item[2] == 2.0 for item in requested)
    assert json.loads(report_path.read_text(encoding="utf-8"))["verified"] is True


def test_verifier_persists_mismatch_before_failing(tmp_path) -> None:
    expected = b"expected"
    manifest = write_manifest(
        tmp_path,
        [
            {
                "path": "index.html",
                "size": len(expected),
                "sha256": hashlib.sha256(expected).hexdigest(),
            }
        ],
    )
    report_path = tmp_path / "verification.json"

    with pytest.raises(fixture.FixtureVerificationError) as raised:
        fixture.verify_fixture(
            "https://fixtures.example/static",
            manifest,
            report_path=report_path,
            fetch=lambda url, _size, _timeout: (b"changed!", url),
        )

    assert "index.html" in str(raised.value)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["verified"] is False
    assert report["verified_file_count"] == 0
    assert report["files"][0]["actual_sha256"] == hashlib.sha256(
        b"changed!"
    ).hexdigest()


def test_manifest_rejects_parent_path_before_fetch(tmp_path) -> None:
    manifest = write_manifest(
        tmp_path,
        [
            {
                "path": "../secret",
                "size": 1,
                "sha256": "a" * 64,
            }
        ],
    )

    with pytest.raises(
        fixture.FixtureVerificationError,
        match="unsafe path",
    ):
        fixture.verify_fixture(
            "https://fixtures.example/static",
            manifest,
            fetch=lambda *_args: pytest.fail("unsafe path was fetched"),
        )


def test_committed_manifest_pins_the_fixture_repository_revision() -> None:
    manifest = json.loads(
        fixture.DEFAULT_MANIFEST.read_text(encoding="utf-8")
    )

    # Static fixtures are versioned inside this repository (pages/) and
    # published through its GitHub Pages; no external fixture repo is pinned.
    assert manifest["source"] == {
        "repository": "https://github.com/lexmount/Lexbench-Headless-Browser",
        "path": "pages",
    }
    assert manifest["deployment_base_url"] == (
        "https://lexmount.github.io/Lexbench-Headless-Browser"
    )
    assert len(manifest["files"]) == 19
    assert {
        "v1/network/data.json",
        "v1/frames/grandchild.html",
        "v1/workers/sw.js",
        "v1/security/security.js",
    } <= {item["path"] for item in manifest["files"]}


def test_committed_manifest_matches_the_in_repo_pages_tree() -> None:
    import hashlib
    import pathlib

    manifest = json.loads(fixture.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    pages_root = fixture.REPO_ROOT / "pages"

    for item in manifest["files"]:
        path = pages_root / item["path"]
        assert path.is_file(), item["path"]
        payload = path.read_bytes()
        assert len(payload) == item["size"], item["path"]
        assert hashlib.sha256(payload).hexdigest() == item["sha256"], item["path"]
