"""Reject imports from another checkout before loading runner configuration."""
from importlib.util import find_spec
from pathlib import Path


def require_local_runner(root: Path) -> None:
    spec = find_spec("runner")
    if spec is None or spec.origin is None:
        raise SystemExit(f"Runner is not installed. Install this checkout: pip install -e {root}")
    if Path(spec.origin).resolve().parent != root / "runner":
        raise SystemExit(f"Runner comes from a different checkout: {spec.origin}. "
                         f"Install this checkout: pip install -e {root}")
