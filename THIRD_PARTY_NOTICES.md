# Third-party notices

Lexbench-Headless-Browser is licensed under Apache-2.0. Some benchmark tasks and
self-hosted fixtures were rewritten from Web Platform Tests (WPT), or designed
with reference to Chromium inspector-protocol tests. The benchmark does not fetch
or execute either upstream suite at run time; the checked-in task, fixture, and
grader files are the complete runnable corpus.

This file keeps the upstream attribution and license text in one place. The paths
below identify the public upstream material associated with each checked-in task.

## Web Platform Tests

Upstream: <https://github.com/web-platform-tests/wpt>

| Task | WPT source path(s) |
|:---|:---|
| `v2_idb_blob_value` | `IndexedDB/blob-valid-before-commit.htm` |
| `v2_idb_compound_key` | `IndexedDB/keypath.htm`<br>`IndexedDB/keyorder.htm` |
| `v2_idb_cursor_advance` | `IndexedDB/idbcursor_advance_objectstore.htm` |
| `v2_idb_cursor_forward` | `IndexedDB/idbcursor_continue_objectstore.htm` |
| `v2_idb_cursor_reverse` | `IndexedDB/idbcursor-direction-objectstore.htm` |
| `v2_idb_getall_keys_count` | `IndexedDB/idbobjectstore_getAllKeys.html`<br>`IndexedDB/idbobjectstore_count.htm` |
| `v2_idb_index_query` | `IndexedDB/idbindex_getAll.html` |
| `v2_idb_index_range_count` | `IndexedDB/idbindex_count.htm`<br>`IndexedDB/idbindex_getAllKeys.html` |
| `v2_idb_keyrange_bound` | `IndexedDB/idbkeyrange.htm` |
| `v2_idb_keyrange_only_upper` | `IndexedDB/idbkeyrange.htm` |
| `v2_idb_multientry_index` | `IndexedDB/idbindex-multientry-arraykeypath.htm` |
| `v2_idb_multistore_atomic` | `IndexedDB/transaction-scope.html` |
| `v2_idb_nested_keypath` | `IndexedDB/keypath.htm` |
| `v2_idb_txn_abort_rollback` | `IndexedDB/transaction-abort.htm` |
| `v2_idb_upgrade_migrate` | `IndexedDB/idbfactory_open9.htm`<br>`IndexedDB/idbversionchangeevent.htm` |
| `v2_wpt_crypto_aesgcm_roundtrip` | `WebCryptoAPI/encrypt_decrypt/aes_gcm.https.any.js` |
| `v2_wpt_crypto_digest_bytelength` | `WebCryptoAPI/digest/digest.https.any.js` |
| `v2_wpt_crypto_getrandomvalues_shape` | `WebCryptoAPI/getRandomValues.any.js` |
| `v2_wpt_crypto_hmac_roundtrip` | `WebCryptoAPI/sign_verify/hmac.https.any.js` |
| `v2_wpt_crypto_hmac_tamper` | `WebCryptoAPI/sign_verify/hmac.https.any.js` |
| `v2_wpt_crypto_sha1_abc` | `WebCryptoAPI/digest/digest.https.any.js` |
| `v2_wpt_crypto_sha256_abc` | `WebCryptoAPI/digest/digest.https.any.js` |
| `v2_wpt_crypto_sha256_empty` | `WebCryptoAPI/digest/digest.https.any.js` |
| `v2_wpt_crypto_sha384_abc` | `WebCryptoAPI/digest/digest.https.any.js` |
| `v2_wpt_crypto_sha512_abc` | `WebCryptoAPI/digest/digest.https.any.js` |
| `v2_wpt_css_at_property` | `css/css-properties-values-api/at-property.html` |
| `v2_wpt_css_cascade_important` | `css/css-cascade/important-vs-inline-001.html` |
| `v2_wpt_css_constructable` | `css/cssom/CSSStyleSheet-constructable.html` |
| `v2_wpt_css_focus_within` | `css/selectors/focus-within-001.html` |
| `v2_wpt_css_has_forgiving` | `css/selectors/has-specificity.html` |
| `v2_wpt_css_is_specificity` | `css/selectors/is-specificity.html` |
| `v2_wpt_css_nth_child_of` | `css/selectors/nth-child-of-classname.html` |
| `v2_wpt_css_property_inherits` | `css/css-properties-values-api/registered-properties-inheritance.html` |
| `v2_wpt_css_register_syntax` | `css/css-properties-values-api/register-property-syntax-parsing.html` |
| `v2_wpt_css_where_zero` | `css/selectors/is-where-basic.html` |
| `v2_wpt_dom_abortsignal` | `dom/abort/event.any.js` |
| `v2_wpt_dom_adoptnode` | `dom/nodes/Document-adoptNode.html`<br>`dom/nodes/Document-importNode.html` |
| `v2_wpt_dom_clonenode` | `dom/nodes/Node-cloneNode.html` |
| `v2_wpt_dom_domparser` | `domparsing/DOMParser-parseFromString-xml-doctype.html`<br>`domparsing/DOMParser-parseFromString-html.html` |
| `v2_wpt_dom_event_order` | `dom/events/Event-dispatch-order.html` |
| `v2_wpt_dom_event_stopprop` | `dom/events/Event-dispatch-propagation-stopped.html` |
| `v2_wpt_dom_mo_attributes` | `dom/nodes/MutationObserver-attributes.html` |
| `v2_wpt_dom_mo_chardata` | `dom/nodes/MutationObserver-characterData.html` |
| `v2_wpt_dom_mo_childlist` | `dom/nodes/MutationObserver-childList.html` |
| `v2_wpt_dom_nodeiterator` | `dom/traversal/NodeIterator.html` |
| `v2_wpt_dom_range_extract` | `dom/ranges/Range-extractContents.html` |
| `v2_wpt_dom_range_surround` | `dom/ranges/Range-surroundContents.html` |
| `v2_wpt_dom_template_content` | `html/semantics/scripting-1/the-template-element/template-element/template-content.html` |
| `v2_wpt_dom_treewalker_filter` | `dom/traversal/TreeWalker-acceptNode-filter.html` |
| `v2_wpt_enc_bom_strip` | `encoding/textdecoder-byte-order-marks.any.js` |
| `v2_wpt_enc_encode_into` | `encoding/encodeInto.any.js` |
| `v2_wpt_enc_encoding_prop` | `encoding/api-basics.any.js` |
| `v2_wpt_enc_fatal_throws` | `encoding/textdecoder-fatal.any.js` |
| `v2_wpt_enc_labels` | `encoding/textdecoder-labels.any.js` |
| `v2_wpt_enc_nonfatal_replacement` | `encoding/textdecoder-fatal.any.js` |
| `v2_wpt_enc_stream_split` | `encoding/streams/decode-utf8.any.js`<br>`encoding/textdecoder-stream.any.js` |
| `v2_wpt_enc_surrogate_replace` | `encoding/api-surrogates-utf8.any.js` |
| `v2_wpt_enc_utf8_astral` | `encoding/api-basics.any.js` |
| `v2_wpt_store_coerce_string` | `webstorage/storage_setitem.window.js` |
| `v2_wpt_store_cookie_multiple` | `cookies/name/name.html`<br>`cookies/value/value.html` |
| `v2_wpt_store_cookie_readwrite` | `cookies/value/value.html` |
| `v2_wpt_store_event_fires` | `webstorage/event_basic.html`<br>`webstorage/event_local_newvalue.html` |
| `v2_wpt_store_key_order` | `webstorage/storage_key.window.js` |
| `v2_wpt_store_local_setget` | `webstorage/storage_setitem.window.js`<br>`webstorage/storage_removeitem.window.js`<br>`webstorage/storage_key.window.js` |
| `v2_wpt_store_overwrite_length` | `webstorage/storage_setitem.window.js` |
| `v2_wpt_store_removeitem_missing` | `webstorage/storage_removeitem.window.js` |
| `v2_wpt_store_session_local_independent` | `webstorage/storage_getitem.window.js`<br>`webstorage/event_session_key.html` |
| `v2_wpt_url_backslash` | `url/url-constructor.any.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_default_port_http` | `url/url-constructor.any.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_empty_query` | `url/url-constructor.any.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_idn_punycode` | `url/url-constructor.any.js`<br>`url/toascii.window.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_lowercase_host` | `url/url-constructor.any.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_nonspecial_opaque` | `url/url-constructor.any.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_pct_space_path` | `url/url-constructor.any.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_protocol_relative` | `url/url-constructor.any.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_rel_dot` | `url/url-constructor.any.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_rel_dotdot` | `url/url-constructor.any.js`<br>`url/resources/urltestdata.json` |
| `v2_wpt_url_sp_getall` | `url/urlsearchparams-getall.any.js` |
| `v2_wpt_url_sp_plus_space` | `url/urlsearchparams-constructor.any.js` |
| `v2_wpt_url_sp_set_replace` | `url/urlsearchparams-set.any.js` |
| `v2_wpt_url_sp_sort` | `url/urlsearchparams-sort.any.js` |

### WPT license (BSD-3-Clause)

Copyright © web-platform-tests contributors

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors may
   be used to endorse or promote products derived from this software without
   specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
OF THE POSSIBILITY OF SUCH DAMAGE.

## Chromium inspector-protocol tests

Upstream: <https://chromium.googlesource.com/chromium/src/>

| Upstream area | Associated task IDs |
|:---|:---|
| `chromium inspector-protocol accessibility/` | `v2_ax_aria_hidden_ignored`<br>`v2_ax_checkbox_checked_state`<br>`v2_ax_control_disabled_state`<br>`v2_ax_depth_truncation`<br>`v2_ax_enable_disable_idempotent`<br>`v2_ax_heading_level_property`<br>`v2_ax_iframe_frameid_scope`<br>`v2_ax_name_arialabel_priority`<br>`v2_ax_name_from_label_association`<br>`v2_ax_query_by_name`<br>`v2_ax_query_by_role`<br>`v2_ax_role_name_value_triples` |
| `chromium inspector-protocol input/` | `v2_act_dispatchkey_listener`<br>`v2_act_inserttext_value` |
| `chromium inspector-protocol runtime/` | `v2_act_binding_execution_context`<br>`v2_act_binding_roundtrip`<br>`v2_act_callfn_arguments`<br>`v2_act_callfn_await_promise`<br>`v2_act_callfn_element_click`<br>`v2_act_callfn_exception`<br>`v2_act_callfn_return_by_value`<br>`v2_act_getprops_accessor`<br>`v2_act_getprops_own`<br>`v2_act_release_object`<br>`v2_act_release_object_group` |
| `chromium test/inspector-protocol/fetch/` | `v2_fetch_continue_request_modify_headers`<br>`v2_fetch_continue_request_modify_url`<br>`v2_fetch_continue_with_auth_probe`<br>`v2_fetch_disable_stops_pausing`<br>`v2_fetch_enable_no_patterns`<br>`v2_fetch_enable_patterns_continue`<br>`v2_fetch_fail_request`<br>`v2_fetch_fail_request_alt_reason`<br>`v2_fetch_fulfill_custom_response`<br>`v2_fetch_fulfill_json`<br>`v2_fetch_fulfill_status_override_404`<br>`v2_fetch_load_network_resource`<br>`v2_fetch_network_get_response_body`<br>`v2_fetch_network_relative_ordering`<br>`v2_fetch_request_paused_request_stage_fields`<br>`v2_fetch_response_stage_take_stream`<br>`v2_ifx_block_resource_fallback`<br>`v2_ifx_header_rewrite_client`<br>`v2_ifx_json_field_inject`<br>`v2_ifx_mock_api_rewrite`<br>`v2_ifx_multi_intercept`<br>`v2_ifx_request_rewrite_url`<br>`v2_ifx_response_body_tamper`<br>`v2_ifx_status_override_branch` |

### Chromium license (BSD-3-Clause)

Copyright 2015 The Chromium Authors

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of Google LLC nor the names of its contributors may be used
   to endorse or promote products derived from this software without specific
   prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
OF THE POSSIBILITY OF SUCH DAMAGE.
