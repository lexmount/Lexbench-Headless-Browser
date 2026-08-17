# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `pydoll` | 92 | 92 | `{"fail": 52, "pass": 40}` | p50=11674 ms; p95=19566 ms; max=29924 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `pydoll` | `sc_app_add_single_item__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_app_cart_persists_reload__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_app_cart_total_two_items__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_app_checkout_receipt__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_app_filter_narrows__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_app_filter_then_add__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_app_locked_without_login__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_app_login_greets__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_app_remove_item__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_computed_style_breadth__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_cs_second_page_independent_nav__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_cs_session_survives_three_pages__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_cs_ua_surface__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_dyn_counter_clicks__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_form_checkbox_event__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_form_focus_active_element__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_form_label_click_toggles__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_form_press_enter_handler__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_form_select_option__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_form_submit_roundtrip__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_fr_child_posts_parent__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_fr_child_self_navigation__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_fr_cross_origin_blocked__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_fr_dynamic_iframe_attach__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_fr_frame_tree_shape__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_fr_nested_two_levels__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_fr_sibling_isolation__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_fr_static_child_read__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_loc_adjacent_sibling__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_loc_attribute_selector_click__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_loc_descendant_scope__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_loc_first_match_semantics__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_loc_iframe_same_origin_read__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_loc_iframe_scope_top_document__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_loc_nth_child_text__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_locator_textbox_len__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_multi_tab_collect__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_nav_back_restores__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_nav_cross_page__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_nav_forward_after_back__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_nav_redirect_chain__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_nav_reload_title__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_nav_slow_document__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_nav_title_read__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_net_route_passthrough__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_st_local_shared_same_origin__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_st_local_survives_reload__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_wait_enabled_then_click__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_wait_late_item__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_wait_slow_badge__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_wait_spinner_replaced__pyd` | 1 | `fail` | answer did not match server-side expectation |
| `pydoll` | `sc_wait_two_phase_settle__pyd` | 1 | `fail` | answer did not match server-side expectation |
