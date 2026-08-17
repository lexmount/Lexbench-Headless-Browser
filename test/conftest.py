"""Shared pytest fixtures for the runner test suite.

Covers TESTING.md §10 (tests layout): bench-root import path, FakeCDP server
factory, FixtureServer factory, and tmp manifest/task bench factories.

All runs/artifacts are written under pytest ``tmp_path`` — tests never touch
the real ``runs/`` or ``tasks/`` trees, never need engine binaries, and never
talk to anything but 127.0.0.1.
"""
from __future__ import annotations

import itertools
import pathlib
import sys

TEST_DIR = pathlib.Path(__file__).resolve().parent
BENCH_ROOT = TEST_DIR.parent
for _p in (str(BENCH_ROOT), str(TEST_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from runner import run as runner_run  # noqa: E402
import _fakes  # noqa: E402


@pytest.fixture
def fake_cdp():
    """Factory: fake_cdp(overrides) -> started FakeCDP (auto-stopped)."""
    servers: list[_fakes.FakeCDP] = []

    def _make(overrides=None) -> _fakes.FakeCDP:
        server = _fakes.FakeCDP(_fakes.method_responder(overrides))
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()


@pytest.fixture
def fixture_server(tmp_path):
    """Factory: fixture_server(fixtures_dir=None, expected_answers=None) -> started FixtureServer."""
    servers: list[runner_run.FixtureServer] = []
    counter = itertools.count()

    def _make(fixtures_dir: pathlib.Path | None = None, expected_answers=None) -> runner_run.FixtureServer:
        if fixtures_dir is None:
            fixtures_dir = tmp_path / f"fixtures{next(counter)}"
            fixtures_dir.mkdir(parents=True, exist_ok=True)
        server = runner_run.FixtureServer(fixtures_dir=fixtures_dir, expected_answers=expected_answers)
        server.start()
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()


@pytest.fixture
def bench_factory(tmp_path):
    """Factory: bench_factory(l1_tasks=[...], l2_tasks=[...], manifest_mut=fn) -> manifest path."""
    counter = itertools.count()

    def _make(l1_tasks=(), l2_tasks=(), manifest_mut=None) -> pathlib.Path:
        root = tmp_path / f"bench{next(counter)}"
        return _fakes.write_bench(root, list(l1_tasks), list(l2_tasks), manifest_mut)

    return _make
