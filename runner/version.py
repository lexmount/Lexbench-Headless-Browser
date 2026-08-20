"""Harness version: the version of the code that runs the benchmark.

Deliberately separate from `manifest.json`'s `bench_version`, which identifies
the dataset — which tasks exist, which graders and fixtures they run against.
The two move for different reasons: a harness fix does not change what is being
measured, and a dataset change does not require new code. Collapsing them would
cost the reader the one question a version number here is meant to answer: are
two runs comparable? See docs/RESULTS.md.

Single source of truth: `pyproject.toml` reads it from here, and a unit test
holds `package.json` to the same value.
"""

HARNESS_VERSION = "0.1.0"
