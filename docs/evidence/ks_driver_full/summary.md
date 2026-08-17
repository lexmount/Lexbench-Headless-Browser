# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `cdp_use` | 92 | 92 | `{"fail": 19, "pass": 73}` | p50=8939 ms; p95=15760 ms; max=21113 ms |
| `chrome_remote_interface` | 92 | 92 | `{"fail": 18, "pass": 74}` | p50=8514 ms; p95=15434 ms; max=20340 ms |
| `chromedp` | 1 | 1 | `{"infra": 1}` | p50=13050 ms; p95=13050 ms; max=13050 ms |
| `chromiumoxide` | 0 | 0 | `{}` | p50=None ms; p95=None ms; max=None ms |
| `ferrum` | 0 | 0 | `{}` | p50=None ms; p95=None ms; max=None ms |
| `playwright` | 0 | 0 | `{}` | p50=None ms; p95=None ms; max=None ms |
| `puppeteer` | 0 | 0 | `{}` | p50=None ms; p95=None ms; max=None ms |
| `pydoll` | 0 | 0 | `{}` | p50=None ms; p95=None ms; max=None ms |
| `rod` | 0 | 0 | `{}` | p50=None ms; p95=None ms; max=None ms |
| `stagehand` | 0 | 0 | `{}` | p50=None ms; p95=None ms; max=None ms |

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
| `chrome_remote_interface` | `sc_app_cart_persists_reload__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_app_filter_then_add__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_cs_ua_surface__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_form_fill_overwrites__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_form_label_click_toggles__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_fr_child_posts_parent__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_fr_child_self_navigation__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_fr_cross_origin_blocked__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_fr_dynamic_iframe_attach__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_fr_frame_tree_shape__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_fr_nested_two_levels__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_fr_sibling_isolation__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_fr_static_child_read__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_loc_iframe_same_origin_read__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_multi_tab_collect__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_nav_push_state_back__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_st_local_shared_same_origin__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chrome_remote_interface` | `sc_st_local_survives_reload__cri` | 1 | `fail` | answer did not match server-side expectation |
| `chromedp` | `sc_agentloop_ax_id_stable_across_mutation__chromedp` | 1 | `infra` | chromedp target cleanup was not confirmed: map[attempts:[map[attempt:1 confirmed:false error:invalid context success:false target_id:404be3080d154b3d921fba60484b0697] map[attempt:2 confirmed:false error:invalid context success:false target_id:404be3080d154b3d921fba60484b0697] map[attempt:1 confirmed:false error:invalid context success:false target_id:bb79cadffdf84bf1a35167d2a11f48e2] map[attempt:2 |
