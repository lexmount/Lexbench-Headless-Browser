"""TESTING.md §7 (真值表 C) — FixtureServer: static serving + server-side grading.

Covers static fixtures/ serving with content types, routes.json root routes,
.headers.json sidecars, path traversal, the /__grade__/expected_answer
endpoint (modes equals / contains_all / contains / unknown / unregistered),
inventory determinism, and the anti-cheat grading of the IndexedDB inventory
task.
"""
from __future__ import annotations

import http.client
import json

from runner import run as runner_run


def raw_get(base_url: str, path: str):
    host, port = base_url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read(), {k.lower(): v for k, v in resp.getheaders()}
    finally:
        conn.close()


def post_json(base_url: str, path: str, payload):
    return runner_run.http_json(base_url + path, timeout=5.0, method="POST", data=payload)


def make_fixtures(tmp_path):
    fdir = tmp_path / "fix"
    fdir.mkdir()
    (fdir / "hello.html").write_text("<h1>hello</h1>", encoding="utf-8")
    (fdir / "app.js").write_text("console.log(1)", encoding="utf-8")
    (fdir / "blob.bin").write_bytes(b"\x00\x01")
    (fdir / "download.txt").write_text("payload", encoding="utf-8")
    (fdir / "download.txt.headers.json").write_text(
        json.dumps({"Content-Type": "application/x-custom", "X-Fixture-Test": "yes"}), encoding="utf-8"
    )
    (fdir / "routes.json").write_text(json.dumps({"/page2": "hello.html"}), encoding="utf-8")
    return fdir


# --- static serving -----------------------------------------------------------


def test_static_file_serving_and_content_types(tmp_path, fixture_server):
    server = fixture_server(make_fixtures(tmp_path))
    status, body, headers = raw_get(server.base_url, "/fixtures/hello.html")
    assert status == 200
    assert body == b"<h1>hello</h1>"
    assert headers["content-type"].startswith("text/html")

    status, _, headers = raw_get(server.base_url, "/fixtures/app.js")
    assert status == 200
    assert headers["content-type"].startswith("application/javascript")

    status, _, headers = raw_get(server.base_url, "/fixtures/blob.bin")
    assert status == 200
    assert headers["content-type"] == "application/octet-stream"


def test_routes_json_root_routes(tmp_path, fixture_server):
    server = fixture_server(make_fixtures(tmp_path))
    status, body, _ = raw_get(server.base_url, "/page2")
    assert status == 200
    assert body == b"<h1>hello</h1>"


def test_headers_sidecar_overrides_content_type(tmp_path, fixture_server):
    server = fixture_server(make_fixtures(tmp_path))
    status, body, headers = raw_get(server.base_url, "/fixtures/download.txt")
    assert status == 200
    assert body == b"payload"
    assert headers["content-type"] == "application/x-custom"
    assert headers["x-fixture-test"] == "yes"


def test_path_traversal_outside_fixtures_dir_is_404(tmp_path, fixture_server):
    server = fixture_server(make_fixtures(tmp_path))
    status, _, _ = raw_get(server.base_url, "/fixtures/../../../../../../../../etc/passwd")
    assert status == 404


def test_sibling_prefix_traversal_is_blocked(tmp_path, fixture_server):
    # Fixed: read_fixture_file uses Path.is_relative_to, so a sibling directory
    # sharing the fixtures dir as a string prefix (fix vs fixother) must 404.
    fdir = make_fixtures(tmp_path)
    sibling = tmp_path / "fixother"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("leaked", encoding="utf-8")
    server = fixture_server(fdir)
    status, _, _ = raw_get(server.base_url, "/fixtures/../fixother/secret.txt")
    assert status == 404


def test_unknown_paths_404(tmp_path, fixture_server):
    server = fixture_server(make_fixtures(tmp_path))
    assert raw_get(server.base_url, "/nope")[0] == 404
    assert raw_get(server.base_url, "/fixtures/missing.html")[0] == 404
    assert post_json_status(server.base_url, "/__unknown__/x") == 404


def post_json_status(base_url: str, path: str) -> int:
    host, port = base_url.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request("POST", path, body=b"{}", headers={"Content-Type": "application/json"})
        return conn.getresponse().status
    finally:
        conn.close()


# --- /__grade__/expected_answer -------------------------------------------------


ANSWERS = {
    "t_eq": {"mode": "equals", "expected": "42"},
    "t_all": {"mode": "contains_all", "expected": ["alpha", "beta"]},
    "t_sub": {"mode": "contains", "expected": "needle"},
    "t_default": {"expected": "7"},  # mode defaults to equals
    "t_badmode": {"mode": "fuzzy", "expected": "x"},
}


def test_expected_answer_equals(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "empty_eq", expected_answers=ANSWERS)
    good = post_json(server.base_url, "/__grade__/expected_answer", {"task_id": "t_eq", "answer": "42"})
    assert good["ok"] is True and good["failure"] is None
    bad = post_json(server.base_url, "/__grade__/expected_answer", {"task_id": "t_eq", "answer": "41"})
    assert bad["ok"] is False
    assert bad["failure"]["class"] == "cdp_semantic"
    assert bad["checks"][0]["name"] == "answer_equals_expected"


def test_expected_answer_default_mode_is_equals(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "empty_def", expected_answers=ANSWERS)
    assert post_json(server.base_url, "/__grade__/expected_answer", {"task_id": "t_default", "answer": "7"})["ok"]


def test_expected_answer_contains_all_reports_missing(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "empty_all", expected_answers=ANSWERS)
    good = post_json(server.base_url, "/__grade__/expected_answer", {"task_id": "t_all", "answer": "beta then alpha"})
    assert good["ok"] is True
    bad = post_json(server.base_url, "/__grade__/expected_answer", {"task_id": "t_all", "answer": "only alpha here"})
    assert bad["ok"] is False
    assert "beta" in bad["checks"][0]["evidence"]


def test_expected_answer_contains(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "empty_sub", expected_answers=ANSWERS)
    assert post_json(server.base_url, "/__grade__/expected_answer", {"task_id": "t_sub", "answer": "hay needle stack"})["ok"]
    assert not post_json(server.base_url, "/__grade__/expected_answer", {"task_id": "t_sub", "answer": "haystack"})["ok"]


def test_expected_answer_unknown_mode_fails(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "empty_bad", expected_answers=ANSWERS)
    result = post_json(server.base_url, "/__grade__/expected_answer", {"task_id": "t_badmode", "answer": "x"})
    assert result["ok"] is False
    assert "unknown mode" in result["checks"][0]["evidence"]


def test_expected_answer_unregistered_task_is_infra(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "empty_unreg", expected_answers=ANSWERS)
    result = post_json(server.base_url, "/__grade__/expected_answer", {"task_id": "ghost", "answer": "x"})
    assert result["ok"] is False
    assert result["failure"]["class"] == "infra"
    assert result["checks"][0]["name"] == "expected_answer_registered"


# --- inventory fixture: determinism + anti-cheat grading -------------------------


def test_expected_count_is_deterministic_and_in_range():
    counts = [runner_run.FixtureServer.expected_count(f"seed{i}") for i in range(30)]
    assert all(10 <= count <= 99 for count in counts)
    assert len(set(counts)) > 1  # seed actually drives the answer
    assert runner_run.FixtureServer.expected_count("abc") == runner_run.FixtureServer.expected_count("abc")
    assert runner_run.FixtureServer.expected_count("a") != runner_run.FixtureServer.expected_count("b")


def test_inventory_html_embeds_seed_derived_count(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "inv")
    seed = "detseed01"
    expected = runner_run.FixtureServer.expected_count(seed)
    status, body, headers = raw_get(server.base_url, f"/storage/indexeddb_inventory?seed={seed}&session=s1")
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    text = body.decode("utf-8")
    assert f'"count": {expected}' in text
    assert f'"seed": "{seed}"' in text


def _grade_payload(server, seed, session, answer=None, observations=None):
    payload = {
        "task_id": "storage_indexeddb_inventory_001",
        "seed": seed,
        "session": session,
        "answer": str(runner_run.FixtureServer.expected_count(seed)) if answer is None else answer,
    }
    if observations is not None:
        payload["observations"] = observations
    return post_json(server.base_url, "/__grade__/storage_indexeddb_inventory_001", payload)


def _post_event(server, session, event):
    post_json(server.base_url, "/__event__/storage_indexeddb_inventory_001", {"session": session, "event": event})


def test_grade_inventory_correct_answer_and_events_pass(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "inv_ok")
    session = "sess-ok"
    _post_event(server, session, "idb_write")
    _post_event(server, session, "idb_read")
    result = _grade_payload(server, "seedA", session)
    assert result["ok"] is True
    assert result["failure"] is None
    assert [check["status"] for check in result["checks"]] == ["pass", "pass", "pass"]


def test_grade_inventory_wrong_answer_fails(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "inv_bad")
    session = "sess-bad"
    _post_event(server, session, "idb_write")
    _post_event(server, session, "idb_read")
    result = _grade_payload(server, "seedA", session, answer="0")
    assert result["ok"] is False
    assert result["failure"]["class"] == "cdp_semantic"
    checks = {check["name"]: check["status"] for check in result["checks"]}
    assert checks["answer_matches_seed"] == "fail"
    assert checks["indexeddb_write_observed"] == "pass"


def test_grade_inventory_forged_observations_without_events_fails(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "inv_forge")
    session = "sess-forged"  # no events ever posted for this session
    result = _grade_payload(
        server,
        "seedA",
        session,
        observations={"session": session, "indexeddb_write_observed": True, "indexeddb_read_observed": True},
    )
    assert result["ok"] is False
    assert result["failure"]["class"] == "cdp_semantic"
    checks = {check["name"]: check["status"] for check in result["checks"]}
    assert checks["answer_matches_seed"] == "pass"
    assert checks["indexeddb_write_observed"] == "fail"
    assert checks["indexeddb_read_observed"] == "fail"


def test_grade_inventory_no_events_and_no_claims_fails(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "inv_none")
    result = _grade_payload(server, "seedA", "sess-silent", observations={})
    assert result["ok"] is False
    assert result["failure"]["class"] == "cdp_semantic"
    checks = {check["name"]: check["status"] for check in result["checks"]}
    assert checks["answer_matches_seed"] == "pass"
    assert checks["indexeddb_write_observed"] == "fail"
    assert checks["indexeddb_read_observed"] == "fail"


def test_event_sessions_are_isolated(tmp_path, fixture_server):
    server = fixture_server(tmp_path / "inv_iso")
    _post_event(server, "sess-a", "idb_write")
    _post_event(server, "sess-a", "idb_read")
    result = _grade_payload(server, "seedA", "sess-b", observations={})
    assert result["ok"] is False  # sess-a events must not vouch for sess-b
