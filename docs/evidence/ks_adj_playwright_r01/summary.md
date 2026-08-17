# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 49 | 49 | `{"fail": 49}` | p50=17883 ms; p95=26730 ms; max=32879 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_app_login_greets__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_cs_ua_surface__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dlg_alert_message__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dlg_confirm_accept__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dlg_confirm_dismiss__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dlg_prompt_text__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dlg_sequence_two_dialogs__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_checkbox_check__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_checkbox_event__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_fill_overwrites__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_label_click_toggles__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_radio_pick__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_submit_roundtrip__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_child_posts_parent__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_child_self_navigation__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_cross_origin_blocked__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_dynamic_iframe_attach__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_frame_tree_shape__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_nested_two_levels__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_sibling_isolation__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_static_child_read__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_loc_attribute_selector_click__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_loc_iframe_same_origin_read__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_multi_tab_collect__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_nav_push_state_back__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_abort_subresource_image__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_auth_header_roundtrip__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_extra_headers_echo__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_mock_only_matching__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_request_recorded__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_abort_handled__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_content_type__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_mock_json__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_passthrough__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_rewrite_status__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_obs_console_error_captured__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_obs_console_log_captured__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_st_local_shared_same_origin__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_st_local_survives_reload__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_up_upload_filereader__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_wait_enabled_then_click__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `v3_pw_act_check_select` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_act_click_counter` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_cal_compute_product` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_cal_counter_clicks` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_cal_form_checksum` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_cookie_roundtrip` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_eval_content_html` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_obs_console_dialog` | 1 | `fail` | script-reported checks failed |
