# Moli task-scoped layout policy

`--moli-layout auto` selects a Moli launch mode from each frozen task definition before any attempt runs. The policy ID is `contract_layout_v2`. The run manifest records every task ID, definition SHA-256, chosen mode, and a hash of the full assignment list. Each result records the actual `--layout` launch flag. Earlier `contract_layout_v1` receipts retain their original assignments and must not be interpreted using this policy.

Mock layout is allowed only for recognized raw CDP metadata reads: `Browser.getVersion` and `Schema.getDomains`, with their matching feature declarations, empty command parameters, an `about:blank` scene, and protocol-error-only grading. The complete driver and step shapes must be recognized. Additional features, operations, script hooks, scenes, or grading requirements select real layout. In the current 1,928-task corpus, exactly two metadata probes meet this contract.

All Node scripts, framework, WebDriver, MCP, agent-tool, geometry, visibility, computed-style and device-metrics tasks select real layout. An unmatched feature or command is not proof that layout is unnecessary. Extending the allowlist requires an independently justified contract and regression coverage; task names and measured outcomes do not determine eligibility. Optional image, font, and media fetching remains controlled independently by the task's `launch_profile`.

`--layout` computes layout on demand. Changing a worker's launch flags restarts its Moli process; comparisons must include that cold-start cost as well as warm task CPU, memory, traffic, latency, and success.

This conservative policy is a benchmark candidate, not an engine capability or resource-saving claim. Before adopting it for a benchmark, compare it prospectively against all-layout Moli and Chromium on the same frozen tasks, host, seed, and driver pins. Keep the common-denominator all-call resource distribution separate from each engine's successful-call distribution.
