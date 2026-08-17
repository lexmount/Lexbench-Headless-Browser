# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `puppeteer` | 129 | 129 | `{"fail": 40, "pass": 89}` | p50=14171 ms; p95=24639 ms; max=29119 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `puppeteer` | `sc_dl_download_checksum__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_dlg_alert_message__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_dlg_confirm_accept__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_dlg_confirm_dismiss__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_dlg_prompt_text__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_dlg_sequence_two_dialogs__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_form_fill_overwrites__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_form_fill_readback__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_form_label_click_toggles__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_form_submit_roundtrip__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_form_textarea_fill__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_child_posts_parent__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_child_self_navigation__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_cross_origin_blocked__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_cross_origin_frame_evaluate__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_dynamic_iframe_attach__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_frame_tree_shape__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_nested_two_levels__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_oopif_attach_read_detach__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_sibling_isolation__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_fr_static_child_read__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_loc_iframe_same_origin_read__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_locator_textbox_len__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_multi_tab_collect__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_nav_push_state_back__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_net_auth_challenge_then_pass__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_net_auth_header_roundtrip__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_net_extra_headers_echo__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_st_local_shared_same_origin__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_st_local_survives_reload__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_up_upload_filereader__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_up_upload_server_checksum__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `v3_pp_act_fill_form` | 1 | `fail` | script-reported checks failed |
| `puppeteer` | `v3_pp_cal_form_checksum` | 1 | `fail` | script-reported checks failed |
| `puppeteer` | `v3_pp_cal_frame_token` | 1 | `fail` | script-reported checks failed |
| `puppeteer` | `v3_pp_cookie_roundtrip` | 1 | `fail` | script-reported checks failed |
| `puppeteer` | `v3_pp_eval_content_html` | 1 | `fail` | script-reported checks failed |
| `puppeteer` | `v3_pp_eval_input_value` | 1 | `fail` | script-reported checks failed |
| `puppeteer` | `v3_pp_nav_wait_for_selector` | 1 | `fail` | script-reported checks failed |
| `puppeteer` | `v3_pp_obs_console_dialog` | 1 | `fail` | script-reported checks failed |
