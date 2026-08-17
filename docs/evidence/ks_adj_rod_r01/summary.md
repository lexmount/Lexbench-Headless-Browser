# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `rod` | 38 | 38 | `{"fail": 37, "pass": 1}` | p50=21516 ms; p95=28770 ms; max=29922 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `rod` | `sc_app_add_single_item__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_cart_persists_reload__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_cart_total_two_items__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_checkout_receipt__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_filter_narrows__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_filter_then_add__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_full_flow_aggregate__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_login_greets__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_logout_locks__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_qty_accumulates__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_app_remove_item__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_dyn_counter_clicks__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_dyn_paging_final__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_form_checkbox_check__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_form_checkbox_event__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_form_label_click_toggles__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_form_press_enter_handler__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_form_radio_pick__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_form_submit_roundtrip__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_fr_child_posts_parent__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_fr_child_self_navigation__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_fr_cross_origin_blocked__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_fr_dynamic_iframe_attach__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_fr_frame_tree_shape__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_fr_nested_two_levels__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_fr_sibling_isolation__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_fr_static_child_read__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_loc_attribute_selector_click__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_loc_first_match_semantics__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_loc_iframe_same_origin_read__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_multi_tab_collect__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_nav_back_restores__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_nav_forward_after_back__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_nav_reload_resets_dom__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_net_route_passthrough__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_st_local_shared_same_origin__rod` | 1 | `fail` | answer did not match server-side expectation |
| `rod` | `sc_wait_enabled_then_click__rod` | 1 | `fail` | answer did not match server-side expectation |
