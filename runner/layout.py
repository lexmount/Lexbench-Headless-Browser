"""Fixed Moli layout selection and rejection of mixed-layout evidence."""

DEFAULT_MODE = "on"
SERVE_ARGS = {
    "off": (),
    "on": ("--layout",),
    "realtime": ("--realtime",),
}


def policy(mode):
    if mode not in SERVE_ARGS:
        raise ValueError(f"unknown Moli layout mode: {mode}")
    return {"policy_id": "fixed_layout_v1", "layout": mode}


def require_fixed(manifest):
    old = manifest.get("moli_layout_policy") or {}
    if manifest.get("layout_retry") or old.get("try_layout") or old.get("retry_layout"):
        raise ValueError("fixed-layout results must not contain layout retries")
