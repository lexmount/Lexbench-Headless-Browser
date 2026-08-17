from __future__ import annotations

import json

from tools.kitesurf_trace_audit import build_audit


def test_trace_audit_links_results_without_copying_sensitive_params(tmp_path) -> None:
    run = tmp_path / "run"
    trace = run / "artifacts/kitesurf/case/1/cdp.jsonl"
    trace.parent.mkdir(parents=True)
    trace_rows = [
        {
            "direction": "send",
            "id": 1,
            "method": "Browser.getVersion",
            "params": {"credential": "must-not-survive"},
        },
        {
            "direction": "recv",
            "id": 1,
            "method": "Browser.getVersion",
            "result": {
                "product": "Chrome/test",
                "protocolVersion": "1.3",
                "revision": "@test",
                "jsVersion": "1",
                "userAgent": "test-agent",
            },
        },
        {
            "direction": "send",
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {"expression": "secret page value"},
        },
        {
            "direction": "event",
            "method": "Network.requestWillBeSentExtraInfo",
            "params": {"headers": {"x-sensitive": "must-not-survive"}},
        },
        {
            "direction": "recv",
            "id": 2,
            "method": "Runtime.evaluate",
            "error": {"code": -1, "message": "expected test error"},
        },
    ]
    trace.write_text(
        "".join(json.dumps(row) + "\n" for row in trace_rows),
        encoding="utf-8",
    )
    result = {
        "sequence": 1,
        "engine": "kitesurf",
        "case_id": "case",
        "attempt": 1,
        "status": "fail",
        "failure": {
            "layer": "protocol",
            "code": "expected",
            "detail": "objectId must-not-survive",
        },
        "trace": "artifacts/kitesurf/case/1/cdp.jsonl",
        "metrics": {
            "cdp_call_count": 2,
            "cdp_error_count": 1,
            "ws_disconnect_count": 0,
        },
    }
    (run / "results.jsonl").write_text(json.dumps(result) + "\n", encoding="utf-8")
    (run / "summary.json").write_text("{}\n", encoding="utf-8")
    (run / "provenance.json").write_text("{}\n", encoding="utf-8")

    audit = build_audit([("headline", run)])
    serialized = json.dumps(audit)

    assert audit["aggregate"]["trace_count"] == 1
    assert audit["aggregate"]["command_methods"] == {
        "Browser.getVersion": 1,
        "Runtime.evaluate": 1,
    }
    assert audit["traces"][0]["identity"]["product"] == "Chrome/test"
    assert audit["traces"][0]["cdp_errors"] == [
        {
            "method": "Runtime.evaluate",
            "code": -1,
            "message": "expected test error",
        }
    ]
    assert "must-not-survive" not in serialized
    assert "secret page value" not in serialized
