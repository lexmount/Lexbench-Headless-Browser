# Moli task-scoped layout policy

`--moli-layout auto` selects a Moli launch mode from each frozen task definition before any attempt runs. The policy ID is `contract_layout_v1`. The run manifest records every task ID, definition SHA-256, chosen mode, and a hash of the full assignment list. Each result records the actual `--layout` launch flag.

The policy enables Moli's on-demand real layout for framework, WebDriver, MCP, and agent-tool tasks. For raw CDP and Node probes, it also enables layout when declared features or commands require geometry, coordinate input, scrolling, visual state, or browser window dimensions. Other raw CDP and Node probes use Moli's mock-layout mode. Unknown or incomplete task contracts select real layout. Optional image, font, and media fetching remains controlled independently by the task's `launch_profile`.

The goal is to retain real layout for interactions while avoiding it for declared nonvisual protocol and JavaScript probes. `--layout` itself computes layout on demand. Changing a worker's launch flags restarts its Moli process; run comparisons must include that cold-start cost as well as warm task CPU, memory, traffic, latency, and success. A mode selected from observed outcomes would invalidate the comparison.

The policy is a benchmark candidate, not an engine capability claim. Evaluate it against an all-layout Moli run and Chromium on the same frozen tasks, host, seed, and driver pins. Keep the common-denominator all-call resource distribution separate from each engine's successful-call distribution.
