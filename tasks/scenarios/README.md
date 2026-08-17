# Scenario specs

A **scenario spec** is a driver-agnostic task: one fixture scene, a sequence of
intent *ops*, and driver-agnostic checks. The generator
(`python3 -m runner.run scenarios`) expands each spec into one concrete task per
bound driver, so a new driver is a new column — never a re-authored task.

- Source of truth: `tasks/scenarios/<scenario_id>.scenario.json`.
- Generated tasks: `tasks/L1/<driver>/sc_<scenario_id>__<suffix>.json`, each
  carrying a `_generated_from` marker. **Do not hand-edit generated files** —
  edit the spec and regenerate.
- `python3 -m runner.run scenarios --check` (also run inside `validate`) fails if
  any generated file is missing, stale, or orphaned.

## Spec schema

| field | required | notes |
|---|---|---|
| `scenario_id` | yes | matches the filename stem; `[a-z0-9_]` |
| `family` | yes | scenario family slug → `family.<family>` tag |
| `title`, `description` | yes | human text; the driver suffix is appended per binding |
| `layer` | no (default `L1`) | `L1` or `L2` |
| `scene` | yes | `{ "kind": "self_hosted_fixture", "url": "/..." }` |
| `steps` | yes | ordered ops interpreted by `runner/scripts/framework_probe.js` |
| `checks` | no | driver-agnostic checks (`step_ok`, `saved_equals`, `saved_truthy`, …) |
| `cdp_anchors` | no | underlying `cdp.*` features this scenario exercises |
| `drivers` | yes | driver keys from `runner/scenario.py:DRIVER_REGISTRY` |
| `driver_skips` | when a driver is unbound | concrete, API-specific reason for every unbound registry driver |
| `extra_tags` | no | extra `family.*` / `purpose.*` tags |

## Op vocabulary

The ops are the shared Playwright/Puppeteer intent set in `framework_probe.js`
(`goto`, `reload`, `go_back`, `click`, `fill`, `type`, `check`, `select_option`,
`evaluate`, `wait_for_selector`, `text_content`, `input_value`, `count`, …).
URL fields accept `{fixture_url}`, `{fixture_origin}`, and `{fixture_base_url}`.

Agent-observation primitives use the same adapter axis:

- `ax_snapshot` returns the driver's full AX snapshot surface. Raw-CDP clients
  preserve `backendDOMNodeId`; chrome-devtools-mcp and agent-browser return
  their native text snapshots.
- `ax_node_identity` finds one exact `role` + accessible `name` pair. It returns
  the raw `backendDOMNodeId` when exposed, or the MCP `uid` for the explicit
  cross-mutation stability scenario. `compare_to` names a previously saved
  identity and returns a `stable|…` or `changed|…` result.
- `computed_style_breadth` calls `CSS.getComputedStyleForNode`, counts distinct
  properties, and reports required values. `min_property_count` defaults to
  100; `required_properties` defaults to `display`, `visibility`, `opacity`,
  and `pointer-events`.

These are normal operations in existing scenario families. They do not create
a new lane or alter scoring.
