# Fixed Moli layout

Layout is off by default. Select `--moli-layout off` or `--moli-layout on` before a run. The chosen mode applies to every Moli attempt, including failed cases.

Every case executes the configured `k` attempts (normally three). All attempts must pass for a case to pass. Failures do not change layout or schedule extra attempts. `results.jsonl` retains one row per engine, task and attempt, and the manifest records the fixed layout and binary identity.

Compare layout modes as separate runs with identical task inputs and repetition counts. Resource and success summaries use each run's own complete results.
