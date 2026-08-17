# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `stagehand` | 46 | 46 | `{"fail": 45, "transport_error": 1}` | p50=21162 ms; p95=30705 ms; max=31845 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `stagehand` | `sc_app_add_single_item__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_cart_persists_reload__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_cart_total_two_items__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_checkout_receipt__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_filter_narrows__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_filter_then_add__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_full_flow_aggregate__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_locked_without_login__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_login_greets__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_logout_locks__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_qty_accumulates__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_app_remove_item__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_cs_ua_surface__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_dyn_counter_clicks__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_dyn_paging_final__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_form_checkbox_check__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_form_checkbox_event__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_form_fill_overwrites__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_form_label_click_toggles__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_form_press_enter_handler__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_form_radio_pick__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_form_select_option__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_form_submit_roundtrip__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_fr_child_posts_parent__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_fr_child_self_navigation__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_fr_cross_origin_blocked__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_fr_dynamic_iframe_attach__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_fr_frame_tree_shape__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_fr_nested_two_levels__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_fr_sibling_isolation__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_fr_static_child_read__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_loc_adjacent_sibling__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_loc_attribute_selector_click__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_loc_descendant_scope__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_loc_first_match_semantics__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_loc_iframe_same_origin_read__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_loc_shadow_open_pierce__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_locator_textbox_len__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_multi_tab_collect__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_nav_push_state_back__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_nav_redirect_chain__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_nav_slow_document__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_net_route_passthrough__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_obs_ax_snapshot_heading__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_obs_ax_snapshot_label__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_st_local_shared_same_origin__sh` | 1 | `transport_error` | <urlopen error _ssl.c:1012: The handshake operation timed out> |
