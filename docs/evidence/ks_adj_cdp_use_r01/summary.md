# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `cdp_use` | 19 | 19 | `{"fail": 19}` | p50=12811 ms; p95=16977 ms; max=24009 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `cdp_use` | `sc_app_cart_persists_reload__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_app_filter_then_add__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_computed_style_breadth__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_cs_ua_surface__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_form_fill_overwrites__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_form_label_click_toggles__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_fr_child_posts_parent__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_fr_child_self_navigation__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_fr_cross_origin_blocked__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_fr_dynamic_iframe_attach__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_fr_frame_tree_shape__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_fr_nested_two_levels__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_fr_sibling_isolation__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_fr_static_child_read__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_loc_iframe_same_origin_read__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_multi_tab_collect__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_nav_push_state_back__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_st_local_shared_same_origin__cdpu` | 1 | `fail` | answer did not match server-side expectation |
| `cdp_use` | `sc_st_local_survives_reload__cdpu` | 1 | `fail` | answer did not match server-side expectation |
