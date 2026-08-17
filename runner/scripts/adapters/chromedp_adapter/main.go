// chromedp scenario adapter.
//
// Drives the engine under test with the pinned github.com/chromedp/chromedp
// Go package — the Go ecosystem's dominant CDP driver. Speaks
// the abb_scenario_adapter/1 contract (../PROTOCOL.md): payload JSON on
// stdin, framework_probe.js result contract on stdout, mandatory two-layer
// binding gate, shared op vocabulary.
//
// The adapter exercises chromedp's own high-level actions (Navigate, Reload,
// Click, SendKeys, Text, Value, Evaluate, WaitReady) — the column measures
// whether those abstractions hold on each engine. NewRemoteAllocator with
// NoModifyURL connects to the runner-verified browser websocket exactly as
// given and never launches a browser.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/chromedp/cdproto/accessibility"
	"github.com/chromedp/cdproto/browser"
	"github.com/chromedp/cdproto/css"
	"github.com/chromedp/cdproto/dom"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/chromedp"
	"github.com/chromedp/chromedp/kb"
)

type payloadT struct {
	BrowserWS         string           `json:"browser_ws"`
	CDPPort           int              `json:"cdp_port"`
	ExpectProduct     string           `json:"expect_product"`
	ExpectUA          string           `json:"expect_ua"`
	ExpectProductLive string           `json:"expect_product_live"`
	TaskURL           string           `json:"task_url"`
	Steps             []map[string]any `json:"steps"`
	Checks            []map[string]any `json:"checks"`
	ConnectTimeoutMS  int              `json:"connect_timeout_ms"`
	ActionTimeoutMS   int              `json:"action_timeout_ms"`
	TaskTimeoutMS     int              `json:"task_timeout_ms"`
	ArtifactDir       string           `json:"artifact_dir"`
}

type checkRow struct {
	Name     string `json:"name"`
	Status   string `json:"status"`
	Evidence string `json:"evidence"`
}

type stepResult struct {
	OK    bool
	Value any
	Err   string
}

var clientVersion = "v0.16.0" // kept in sync with go.mod

func emit(obj map[string]any) {
	data, err := json.Marshal(obj)
	if err != nil {
		data, _ = json.Marshal(map[string]any{
			"ok":           false,
			"error":        map[string]any{"class": "script_error", "message": "result marshal failed: " + err.Error()},
			"observations": map[string]any{},
			"metrics":      map[string]any{},
		})
	}
	os.Stdout.Write(data)
}

func emitInfra(message string, binding map[string]any, calls, errs int) {
	emit(map[string]any{
		"ok":           false,
		"error":        map[string]any{"class": "script_error", "message": message},
		"observations": map[string]any{"binding": binding},
		"metrics":      map[string]any{"cdp_call_count": calls, "cdp_error_count": errs, "ws_disconnect_count": 0},
	})
}

func toSavedString(value any) string {
	switch v := value.(type) {
	case nil:
		return "null"
	case string:
		return v
	case bool:
		if v {
			return "true"
		}
		return "false"
	case float64:
		return strconv.FormatFloat(v, 'f', -1, 64) // JS Number stringification: 42, 3.5
	default:
		data, err := json.Marshal(value)
		if err != nil {
			return fmt.Sprintf("%v", value)
		}
		return string(data)
	}
}

func isUnsupported(msg string) bool {
	lower := strings.ToLower(msg)
	for _, marker := range []string{"not found", "wasn't found", "unsupported", "unknown method", "not implemented", "not supported"} {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

type adapter struct {
	payload payloadT
	fixture *url.URL
	trace   *os.File
	// rootCtx is the first chromedp context: it owns the browser connection,
	// so it must stay alive for the whole run. Page contexts for new_page are
	// spawned from it and only those get cancelled.
	rootCtx    context.Context
	tabCtx     context.Context
	tabCancels []context.CancelFunc
	saved      map[string]string
	steps      []stepResult
	opCalls    int
	opErrors   int
	// budgetDeadline is the runner's task_ms kill time minus a reserve; ops
	// clamp their waits to it so the adapter always emits a result instead of
	// being killed mid-run (which would misclassify the fail as infra).
	budgetDeadline time.Time
}

func (a *adapter) traceLine(obj map[string]any) {
	if a.trace == nil {
		return
	}
	obj["ts"] = time.Now().UTC().Format(time.RFC3339)
	data, err := json.Marshal(obj)
	if err != nil {
		return
	}
	a.trace.Write(append(data, '\n'))
}

func (a *adapter) substitute(raw string) string {
	text := strings.ReplaceAll(raw, "{fixture_url}", a.payload.TaskURL)
	text = strings.ReplaceAll(text, "{fixture_origin}", a.fixture.Scheme+"://"+a.fixture.Host)
	text = strings.ReplaceAll(text, "{fixture_host}", a.fixture.Host)
	text = strings.ReplaceAll(text, "{artifact_dir}", a.payload.ArtifactDir)
	return text
}

func (a *adapter) clampToBudget(timeoutMS int) (int, error) {
	remaining := int(time.Until(a.budgetDeadline) / time.Millisecond)
	if remaining <= 0 {
		return 0, fmt.Errorf("task budget exhausted before op could run")
	}
	if timeoutMS > remaining {
		return remaining, nil
	}
	return timeoutMS, nil
}

func (a *adapter) run(timeoutMS int, actions ...chromedp.Action) error {
	a.opCalls++
	clamped, err := a.clampToBudget(timeoutMS)
	if err != nil {
		a.opErrors++
		return err
	}
	timeoutMS = clamped
	// chromedp gotcha: cancelling a ctx derived from a chromedp context that
	// performed the target's FIRST Run tears the target down. Timeouts are
	// therefore enforced by racing a goroutine, never by cancelling.
	done := make(chan error, 1)
	go func() { done <- chromedp.Run(a.tabCtx, actions...) }()
	select {
	case err := <-done:
		if err != nil {
			a.opErrors++
		}
		return err
	case <-time.After(time.Duration(timeoutMS) * time.Millisecond):
		a.opErrors++
		return fmt.Errorf("timeout after %dms", timeoutMS)
	}
}

// evalValue evaluates an expression tolerating undefined results (chromedp
// errors on unmarshalling an undefined remote value into a target).
func (a *adapter) evalValue(timeoutMS int, expression string) (any, error) {
	var raw json.RawMessage
	err := a.run(timeoutMS, chromedp.Evaluate(expression, &raw))
	if err != nil {
		if strings.Contains(err.Error(), "undefined") {
			return nil, nil
		}
		return nil, err
	}
	if len(raw) == 0 {
		return nil, nil
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return string(raw), nil
	}
	return value, nil
}

func axValue(value *accessibility.Value) string {
	if value == nil || len(value.Value) == 0 {
		return ""
	}
	var decoded any
	if err := json.Unmarshal(value.Value, &decoded); err != nil {
		return strings.Trim(string(value.Value), `"`)
	}
	return fmt.Sprintf("%v", decoded)
}

func (a *adapter) fullAXTree(timeoutMS int) ([]*accessibility.Node, error) {
	var nodes []*accessibility.Node
	err := a.run(timeoutMS, chromedp.ActionFunc(func(ctx context.Context) error {
		if err := accessibility.Enable().Do(ctx); err != nil {
			return err
		}
		var err error
		nodes, err = accessibility.GetFullAXTree().Do(ctx)
		return err
	}))
	return nodes, err
}

func findAXIdentity(nodes []*accessibility.Node, role, name string) (string, error) {
	for _, node := range nodes {
		if node == nil || axValue(node.Role) != role || axValue(node.Name) != name {
			continue
		}
		if node.BackendDOMNodeID == 0 {
			return "", fmt.Errorf("AX node role=%q name=%q has no backendDOMNodeId", role, name)
		}
		return fmt.Sprintf("role=%s|name=%s|backendDOMNodeId=%d", role, name, node.BackendDOMNodeID), nil
	}
	return "", fmt.Errorf("AX node role=%q name=%q not found", role, name)
}

func formatComputedStyle(step map[string]any, computed []*css.ComputedStyleProperty) string {
	required := []string{"display", "visibility", "opacity", "pointer-events"}
	if raw, ok := step["required_properties"].([]any); ok && len(raw) > 0 {
		required = required[:0]
		for _, item := range raw {
			required = append(required, fmt.Sprintf("%v", item))
		}
	}
	minimum := 100
	if value, ok := step["min_property_count"].(float64); ok {
		minimum = int(value)
	}
	properties := make(map[string]string, len(computed))
	for _, entry := range computed {
		if entry != nil {
			properties[entry.Name] = entry.Value
		}
	}
	readable := true
	details := make([]string, 0, len(required))
	for _, name := range required {
		value, ok := properties[name]
		if !ok || value == "" {
			readable = false
			value = "<missing>"
		}
		details = append(details, name+"="+value)
	}
	prefix := "breadth-insufficient"
	if len(properties) >= minimum && readable {
		prefix = "breadth-ok"
	}
	return fmt.Sprintf("%s|count=%d|%s", prefix, len(properties), strings.Join(details, "|"))
}

func selExpr(sel, body string) string {
	quoted, _ := json.Marshal(sel)
	escaped := strings.ReplaceAll(sel, `"`, `\"`)
	return fmt.Sprintf(`(() => { const el = document.querySelector(%s); if (!el) throw new Error("no element matches %s"); %s })()`, quoted, escaped, body)
}

func (a *adapter) pollUntil(timeoutMS int, expression, what string) error {
	if clamped, err := a.clampToBudget(timeoutMS); err != nil {
		return err
	} else {
		timeoutMS = clamped
	}
	deadline := time.Now().Add(time.Duration(timeoutMS) * time.Millisecond)
	for {
		value, err := a.evalValue(2000, expression)
		if err == nil && value != nil && value != false && value != "" {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("timeout after %dms waiting for %s", timeoutMS, what)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func (a *adapter) settleNavigation(timeoutMS int, target string) error {
	parsed, err := url.Parse(target)
	if err != nil {
		return err
	}
	wantPath, _ := json.Marshal(parsed.Path + queryPart(parsed))
	return a.pollUntil(timeoutMS, fmt.Sprintf(`document.readyState === "complete" && (location.pathname + location.search) === %s`, wantPath), "navigation to "+target)
}

func queryPart(u *url.URL) string {
	if u.RawQuery == "" {
		return ""
	}
	return "?" + u.RawQuery
}

func (a *adapter) runOp(step map[string]any) (any, error) {
	op, _ := step["op"].(string)
	sel := ""
	if s, ok := step["selector"].(string); ok {
		sel = a.substitute(s)
	}
	timeout := a.payload.ActionTimeoutMS
	if t, ok := step["timeout_ms"].(float64); ok {
		timeout = int(t)
	}

	switch op {
	case "wait_ms":
		ms := 100.0
		if v, ok := step["ms"].(float64); ok {
			ms = v
		}
		time.Sleep(time.Duration(ms) * time.Millisecond)
		return nil, nil
	case "version", "user_agent":
		var product, userAgent string
		err := a.run(timeout, chromedp.ActionFunc(func(ctx context.Context) error {
			_, prod, _, ua, _, err := browser.GetVersion().Do(ctx)
			product, userAgent = prod, ua
			return err
		}))
		if err != nil {
			return nil, err
		}
		if op == "version" {
			return product, nil
		}
		return userAgent, nil
	case "new_page":
		// A fresh chromedp context spawned from the root = a new tab in the
		// same browser. Retain prior contexts so multi-tab scenarios measure
		// two simultaneously live targets instead of serial page replacement.
		var tabCancel context.CancelFunc
		a.tabCtx, tabCancel = chromedp.NewContext(a.rootCtx)
		a.tabCancels = append(a.tabCancels, tabCancel)
		// First Run creates the target; make it explicit and cheap.
		if err := a.run(timeout, chromedp.Navigate("about:blank")); err != nil {
			return nil, err
		}
		return "page_created", nil
	case "goto":
		target := a.payload.TaskURL
		if u, ok := step["url"].(string); ok && u != "" {
			target = a.substitute(u)
		}
		if err := a.run(timeout, chromedp.Navigate(target)); err != nil {
			return nil, err
		}
		if err := a.settleNavigation(timeout, target); err != nil {
			return nil, err
		}
		return "navigated", nil
	case "reload":
		if _, err := a.evalValue(timeout, `window.__abb_reload_probe = 1, "marked"`); err != nil {
			return nil, err
		}
		if err := a.run(timeout, chromedp.Reload()); err != nil {
			return nil, err
		}
		if err := a.pollUntil(timeout, `document.readyState === "complete" && !window.__abb_reload_probe`, "reload to settle"); err != nil {
			return nil, err
		}
		return "reloaded", nil
	case "go_back", "go_forward":
		navNonce := fmt.Sprintf("np%d", time.Now().UnixNano())
		if _, err := a.evalValue(timeout, fmt.Sprintf(`window.__abb_nav_probe = %q + "|" + location.href, "marked"`, navNonce)); err != nil {
			return nil, err
		}
		// chromedp.NavigateBack/Forward wait for lifecycle events that never
		// fire on a BFCache restore, so drive the history entry directly and
		// settle via the marker poll instead.
		noHistory := false
		nav := chromedp.ActionFunc(func(ctx context.Context) error {
			idx, entries, err := page.GetNavigationHistory().Do(ctx)
			if err != nil {
				return err
			}
			target := idx - 1
			if op == "go_forward" {
				target = idx + 1
			}
			if target < 0 || target >= int64(len(entries)) {
				noHistory = true
				return nil
			}
			return page.NavigateToHistoryEntry(entries[target].ID).Do(ctx)
		})
		if err := a.run(timeout, nav); err != nil {
			return nil, err
		}
		if noHistory {
			return "no_history", nil
		}
		if err := a.pollUntil(timeout, fmt.Sprintf(`document.readyState === "complete" && window.__abb_nav_probe !== %q + "|" + location.href`, navNonce), op); err != nil {
			return nil, err
		}
		// Upstream chromedp bug (v0.16.0): after a cross-document history
		// traversal that Chrome serves from BFCache, Page.frameNavigated
		// wipes the tracked frame's Root and no DOM.documentUpdated follows,
		// so every DOM-domain action (Text, Click, ...) retries against a
		// nil root forever while Runtime evaluate still works. Detect the
		// cross-document case via the nonce marker (a same-document
		// traversal keeps this op's marker) and repair with an in-place
		// reload — a real navigation that replays the normal event order;
		// the history position is preserved.
		sameDoc, _ := a.evalValue(timeout, fmt.Sprintf(`typeof window.__abb_nav_probe === "string" && window.__abb_nav_probe.indexOf(%q + "|") === 0`, navNonce))
		if sameDoc != true {
			a.traceLine(map[string]any{"direction": "chromedp", "step": "dom_repair_reload", "op": op})
			if err := a.run(timeout, chromedp.Reload()); err != nil {
				return nil, err
			}
			if err := a.pollUntil(timeout, `document.readyState === "complete"`, op+" dom repair"); err != nil {
				return nil, err
			}
		}
		return "ok", nil
	case "click":
		times := 1
		if v, ok := step["times"].(float64); ok {
			times = int(v)
		}
		for i := 0; i < times; i++ {
			if err := a.run(timeout, chromedp.Click(sel, chromedp.ByQuery, chromedp.NodeVisible)); err != nil {
				return nil, err
			}
		}
		return fmt.Sprintf("clicked x%d", times), nil
	case "fill":
		value := ""
		if v, ok := step["value"].(string); ok {
			value = a.substitute(v)
		}
		// chromedp.Clear leaves the old value in place (SendKeys then
		// appends) and SetValue detaches the control's value from typing;
		// select the existing content so the typed keys replace it.
		if err := a.run(timeout, chromedp.Click(sel, chromedp.ByQuery, chromedp.NodeVisible)); err != nil {
			return nil, err
		}
		if _, err := a.evalValue(timeout, selExpr(sel, `el.focus(); if (typeof el.select === "function") el.select(); return "selected";`)); err != nil {
			return nil, err
		}
		if err := a.run(timeout,
			chromedp.SendKeys(sel, value, chromedp.ByQuery),
		); err != nil {
			return nil, err
		}
		return "filled", nil
	case "type":
		text := ""
		if v, ok := step["text"].(string); ok {
			text = a.substitute(v)
		}
		if err := a.run(timeout, chromedp.SendKeys(sel, text, chromedp.ByQuery)); err != nil {
			return nil, err
		}
		return "typed", nil
	case "press":
		key, _ := step["key"].(string)
		mapped := map[string]string{"Enter": kb.Enter, "Tab": kb.Tab, "Escape": kb.Escape, "Backspace": kb.Backspace}[key]
		if mapped == "" {
			if len(key) == 1 {
				mapped = key
			} else {
				return nil, fmt.Errorf("unsupported key %q for press", key)
			}
		}
		if sel != "" {
			if err := a.run(timeout, chromedp.SendKeys(sel, mapped, chromedp.ByQuery)); err != nil {
				return nil, err
			}
		} else {
			if err := a.run(timeout, chromedp.KeyEvent(mapped)); err != nil {
				return nil, err
			}
		}
		return "pressed " + key, nil
	case "check":
		already, err := a.evalValue(timeout, selExpr(sel, "return !!el.checked;"))
		if err != nil {
			return nil, err
		}
		if already != true {
			if err := a.run(timeout, chromedp.Click(sel, chromedp.ByQuery, chromedp.NodeVisible)); err != nil {
				return nil, err
			}
		}
		return "checked", nil
	case "select_option":
		value := ""
		if v, ok := step["value"].(string); ok {
			value = a.substitute(v)
		}
		quotedValue, _ := json.Marshal(value)
		return a.evalValue(timeout, selExpr(sel, fmt.Sprintf(
			`el.value = %s; el.dispatchEvent(new Event("input", {bubbles: true})); el.dispatchEvent(new Event("change", {bubbles: true})); return [el.value];`, quotedValue)))
	case "focus":
		if err := a.run(timeout, chromedp.Focus(sel, chromedp.ByQuery)); err != nil {
			return nil, err
		}
		return "focused", nil
	case "evaluate":
		expr, _ := step["expression"].(string)
		return a.evalValue(timeout, a.substitute(expr))
	case "wait_for_function":
		expr, _ := step["expression"].(string)
		if err := a.pollUntil(timeout, a.substitute(expr), "predicate"); err != nil {
			return nil, err
		}
		return "predicate_true", nil
	case "wait_for_selector":
		state, _ := step["state"].(string)
		quoted, _ := json.Marshal(sel)
		var expression string
		switch state {
		case "hidden", "detached":
			expression = fmt.Sprintf(`(() => { const el = document.querySelector(%s); if (!el) return true; const s = window.getComputedStyle(el); return s.display === "none" || s.visibility === "hidden"; })()`, quoted)
		case "visible":
			expression = fmt.Sprintf(`(() => { const el = document.querySelector(%s); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; })()`, quoted)
		default:
			expression = fmt.Sprintf(`!!document.querySelector(%s)`, quoted)
		}
		if err := a.pollUntil(timeout, expression, "selector "+sel); err != nil {
			return nil, err
		}
		return "selector_ready", nil
	case "text_content":
		var out string
		if err := a.run(timeout, chromedp.Text(sel, &out, chromedp.ByQuery, chromedp.NodeReady)); err != nil {
			return nil, err
		}
		return out, nil
	case "inner_text":
		return a.evalValue(timeout, selExpr(sel, "return el.innerText;"))
	case "get_attribute":
		name, _ := step["name"].(string)
		quotedName, _ := json.Marshal(name)
		return a.evalValue(timeout, selExpr(sel, fmt.Sprintf("return el.getAttribute(%s);", quotedName)))
	case "input_value":
		var out string
		if err := a.run(timeout, chromedp.Value(sel, &out, chromedp.ByQuery)); err != nil {
			return nil, err
		}
		return out, nil
	case "count":
		quoted, _ := json.Marshal(sel)
		return a.evalValue(timeout, fmt.Sprintf("document.querySelectorAll(%s).length", quoted))
	case "is_visible":
		quoted, _ := json.Marshal(sel)
		return a.evalValue(timeout, fmt.Sprintf(`(() => { const el = document.querySelector(%s); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; })()`, quoted))
	case "is_checked":
		return a.evalValue(timeout, selExpr(sel, "return !!el.checked;"))
	case "is_enabled":
		return a.evalValue(timeout, selExpr(sel, "return !el.disabled;"))
	case "ax_snapshot":
		return a.fullAXTree(timeout)
	case "ax_node_identity":
		role, _ := step["role"].(string)
		name := a.substitute(fmt.Sprintf("%v", step["name"]))
		nodes, err := a.fullAXTree(timeout)
		if err != nil {
			return nil, err
		}
		identity, err := findAXIdentity(nodes, role, name)
		if err != nil {
			return nil, err
		}
		if compareTo, ok := step["compare_to"].(string); ok && compareTo != "" {
			before := a.saved[compareTo]
			if before == identity {
				return "stable|" + identity, nil
			}
			return fmt.Sprintf("changed|before=%s|after=%s", before, identity), nil
		}
		return identity, nil
	case "computed_style_breadth":
		var computed []*css.ComputedStyleProperty
		err := a.run(timeout, chromedp.ActionFunc(func(ctx context.Context) error {
			// CSS.enable can refresh the agent's document state. Enable it
			// before resolving the frontend node so the nodeId remains valid
			// for CSS.getComputedStyleForNode.
			if err := css.Enable().Do(ctx); err != nil {
				return err
			}
			root, err := dom.GetDocument().WithDepth(0).Do(ctx)
			if err != nil {
				return err
			}
			nodeID, err := dom.QuerySelector(root.NodeID, sel).Do(ctx)
			if err != nil {
				return err
			}
			if nodeID == 0 {
				return fmt.Errorf("no element matches %s", sel)
			}
			computed, _, err = css.GetComputedStyleForNode(nodeID).Do(ctx)
			return err
		}))
		if err != nil {
			return nil, err
		}
		return formatComputedStyle(step, computed), nil
	case "title":
		var out string
		if err := a.run(timeout, chromedp.Title(&out)); err != nil {
			return nil, err
		}
		return out, nil
	case "url":
		var out string
		if err := a.run(timeout, chromedp.Location(&out)); err != nil {
			return nil, err
		}
		return out, nil
	default:
		return nil, fmt.Errorf("unknown op %q", op)
	}
}

func (a *adapter) evaluateCheck(check map[string]any) (bool, string) {
	kind, _ := check["kind"].(string)
	name, _ := check["name"].(string)
	switch kind {
	case "saved_equals":
		value, present := a.saved[name]
		expected := fmt.Sprintf("%v", check["expected"])
		evidence := fmt.Sprintf("%s=%s expected=%q", name, quoteOrNull(value, present), expected)
		return present && value == expected, evidence
	case "saved_contains":
		value, present := a.saved[name]
		want := a.substitute(fmt.Sprintf("%v", check["expected"]))
		return present && strings.Contains(value, want), fmt.Sprintf("%s=%s must contain %q", name, quoteOrNull(truncate(value, 300), present), want)
	case "saved_not_contains":
		value, present := a.saved[name]
		want := a.substitute(fmt.Sprintf("%v", check["expected"]))
		return present && !strings.Contains(value, want), fmt.Sprintf("%s=%s must NOT contain %q", name, quoteOrNull(truncate(value, 300), present), want)
	case "saved_truthy":
		value, present := a.saved[name]
		truthy := present && value != "" && value != "undefined" && value != "null" && value != "false" && !strings.HasPrefix(value, "ERROR:")
		return truthy, fmt.Sprintf("%s=%s", name, quoteOrNull(truncate(value, 300), present))
	case "step_ok", "step_fails":
		idx := -1
		if v, ok := check["step"].(float64); ok {
			idx = int(v)
		}
		var row stepResult
		if idx >= 0 && idx < len(a.steps) {
			row = a.steps[idx]
		}
		errText := row.Err
		if errText == "" {
			errText = "none"
		}
		if kind == "step_ok" {
			return row.OK, fmt.Sprintf("step %d ok=%v error=%s", idx, row.OK, errText)
		}
		return !row.OK, fmt.Sprintf("step %d ok=%v (must fail) error=%s", idx, row.OK, errText)
	case "file_nonempty":
		path := a.substitute(fmt.Sprintf("%v", check["path"]))
		info, err := os.Stat(path)
		size := int64(0)
		if err == nil {
			size = info.Size()
		}
		return size > 0, fmt.Sprintf("%s size=%d", path, size)
	case "any_of":
		subs, _ := check["checks"].([]any)
		pieces := make([]string, 0, len(subs))
		anyOK := false
		for _, sub := range subs {
			subMap, _ := sub.(map[string]any)
			ok, evidence := a.evaluateCheck(subMap)
			if ok {
				anyOK = true
			}
			status := "fail"
			if ok {
				status = "pass"
			}
			pieces = append(pieces, status+": "+evidence)
		}
		return anyOK, strings.Join(pieces, " | ")
	default:
		return false, fmt.Sprintf("unknown check kind %v", check["kind"])
	}
}

func quoteOrNull(value string, present bool) string {
	if !present {
		return "null"
	}
	return fmt.Sprintf("%q", value)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func checkName(check map[string]any, idx int) string {
	if label, ok := check["label"].(string); ok && label != "" {
		return label
	}
	if kind, ok := check["kind"].(string); ok && kind != "" {
		return kind
	}
	return fmt.Sprintf("check%d", idx)
}

func httpJSON(rawURL string, timeout time.Duration) (map[string]any, error) {
	client := &http.Client{Timeout: timeout}
	resp, err := client.Get(rawURL)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("invalid JSON from %s: %w", rawURL, err)
	}
	return out, nil
}

func main() {
	stdin, err := io.ReadAll(os.Stdin)
	if err != nil {
		emitInfra("cannot read stdin: "+err.Error(), nil, 0, 0)
		return
	}
	var payload payloadT
	if err := json.Unmarshal(stdin, &payload); err != nil {
		emitInfra("invalid payload JSON on stdin: "+err.Error(), nil, 0, 0)
		return
	}
	if payload.ConnectTimeoutMS == 0 {
		payload.ConnectTimeoutMS = 15000
	}
	if payload.ActionTimeoutMS == 0 {
		payload.ActionTimeoutMS = 8000
	}
	if payload.TaskTimeoutMS == 0 {
		payload.TaskTimeoutMS = 30000
	}
	binding := map[string]any{
		"driver": "chromedp", "browser_ws": payload.BrowserWS,
		"expect_product": payload.ExpectProduct, "verified": false, "gate": nil,
		"client_version": clientVersion,
	}
	if payload.BrowserWS == "" || payload.TaskURL == "" {
		emitInfra("payload requires browser_ws and task_url", binding, 0, 0)
		return
	}
	fixture, err := url.Parse(payload.TaskURL)
	if err != nil {
		emitInfra("invalid task_url: "+err.Error(), binding, 0, 0)
		return
	}

	os.MkdirAll(payload.ArtifactDir, 0o755)
	traceFile, _ := os.Create(filepath.Join(payload.ArtifactDir, "cdp.jsonl"))
	if traceFile != nil {
		defer traceFile.Close()
	}
	a := &adapter{payload: payload, fixture: fixture, trace: traceFile, saved: map[string]string{}}
	// Leave a 3s reserve for check evaluation and result emission.
	a.budgetDeadline = time.Now().Add(time.Duration(payload.TaskTimeoutMS-3000) * time.Millisecond)

	// ---- Binding gate 1/2: HTTP identity of the endpoint, verbatim.
	if payload.CDPPort == 0 || payload.ExpectProduct == "" {
		emitInfra("binding gate: cdp_port and expect_product are required (refusing to run unverified)", binding, 0, 0)
		return
	}
	versionInfo, err := httpJSON(fmt.Sprintf("http://127.0.0.1:%d/json/version", payload.CDPPort), 4*time.Second)
	if err != nil {
		emitInfra(fmt.Sprintf("binding gate: /json/version unreachable on port %d: %s", payload.CDPPort, err), binding, 0, 0)
		return
	}
	httpProduct, _ := versionInfo["Browser"].(string)
	if httpProduct == "" {
		httpProduct, _ = versionInfo["Product"].(string)
	}
	httpUA, _ := versionInfo["User-Agent"].(string)
	binding["http_product"] = httpProduct
	if httpProduct != payload.ExpectProduct || (payload.ExpectUA != "" && httpUA != payload.ExpectUA) {
		emitInfra(fmt.Sprintf("binding gate: endpoint on port %d reports product=%q ua=%q; expected product=%q — refusing to run against an unverified engine", payload.CDPPort, httpProduct, httpUA, payload.ExpectProduct), binding, 0, 0)
		return
	}
	if wsFromVersion, _ := versionInfo["webSocketDebuggerUrl"].(string); wsFromVersion != "" && wsFromVersion != payload.BrowserWS {
		emitInfra(fmt.Sprintf("binding gate: browser_ws %s != verified endpoint %s", payload.BrowserWS, wsFromVersion), binding, 0, 0)
		return
	}
	binding["gate"] = "http_json_version"

	// ---- Connect. NoModifyURL: use the verified websocket exactly as given.
	allocCtx, allocCancel := chromedp.NewRemoteAllocator(context.Background(), payload.BrowserWS, chromedp.NoModifyURL)
	defer allocCancel()
	rootCtx, rootCancel := chromedp.NewContext(allocCtx)
	defer rootCancel()
	a.rootCtx = rootCtx
	a.tabCtx = rootCtx
	defer func() {
		for i := len(a.tabCancels) - 1; i >= 0; i-- {
			a.tabCancels[i]()
		}
	}()

	// ---- Binding gate 2/2: live-transport identity. The first Run also
	// creates the tab, so a connect failure surfaces here.
	expectLive := payload.ExpectProductLive
	if expectLive == "" {
		expectLive = payload.ExpectProduct
	}
	var liveProduct string
	connectErr := a.run(payload.ConnectTimeoutMS, chromedp.ActionFunc(func(ctx context.Context) error {
		_, product, _, _, _, err := browser.GetVersion().Do(ctx)
		liveProduct = product
		return err
	}))
	a.traceLine(map[string]any{"direction": "chromedp", "step": "connect", "ok": connectErr == nil, "error": errText(connectErr)})
	if connectErr != nil {
		// A refused/failed connect is a genuine compatibility result: the
		// engine cannot be driven by this client. Grade every check as failed.
		rows := []checkRow{{Name: "driver_connect", Status: "fail", Evidence: fmt.Sprintf("chromedp@%s could not drive %s: %s", clientVersion, payload.BrowserWS, truncate(connectErr.Error(), 500))}}
		for idx, check := range payload.Checks {
			rows = append(rows, checkRow{Name: checkName(check, idx), Status: "fail", Evidence: "client did not connect; scenario not executed"})
		}
		emit(map[string]any{
			"ok":     true,
			"answer": fmt.Sprintf("0/%d checks", len(rows)),
			"observations": map[string]any{
				"checks": rows, "saved": map[string]string{}, "binding": binding,
				"connect_error": truncate(connectErr.Error(), 500), "failure_class": "cdp_semantic",
			},
			"metrics": map[string]any{"cdp_call_count": 1, "cdp_error_count": 1, "ws_disconnect_count": 0},
		})
		return
	}
	binding["expect_product_live"] = expectLive
	binding["live_product"] = liveProduct
	binding["live_check"] = "chromedp_browser_get_version"
	if liveProduct != expectLive {
		emitInfra(fmt.Sprintf("binding gate: live chromedp transport reports product=%q; expected %q — the client is not bound to the engine under test", liveProduct, expectLive), binding, a.opCalls, a.opErrors)
		return
	}
	binding["verified"] = true
	a.traceLine(map[string]any{"direction": "chromedp", "step": "binding_verified", "product": liveProduct})

	// ---- Scenario steps.
	for idx, step := range payload.Steps {
		value, err := a.runOp(step)
		result := stepResult{OK: err == nil, Value: value}
		if err != nil {
			result.Err = truncate(err.Error(), 1000)
		}
		a.steps = append(a.steps, result)
		a.traceLine(map[string]any{"direction": "chromedp", "step": idx, "op": step["op"], "selector": step["selector"], "ok": result.OK, "error": result.Err})
		if saveAs, ok := step["save_as"].(string); ok && saveAs != "" {
			if result.OK {
				if value == nil {
					a.saved[saveAs] = "undefined"
				} else {
					a.saved[saveAs] = toSavedString(value)
				}
			} else {
				a.saved[saveAs] = "ERROR: " + result.Err
			}
		}
	}

	rows := []checkRow{{Name: "driver_connect", Status: "pass", Evidence: fmt.Sprintf("chromedp@%s bound to %s", clientVersion, liveProduct)}}
	passCount := 1
	for idx, check := range payload.Checks {
		ok, evidence := a.evaluateCheck(check)
		status := "fail"
		if ok {
			status = "pass"
			passCount++
		}
		rows = append(rows, checkRow{Name: checkName(check, idx), Status: status, Evidence: evidence})
	}

	answer := fmt.Sprintf("%d/%d checks", passCount, len(rows))
	if v, ok := a.saved["answer"]; ok {
		answer = v
	}
	driverOpErrors := 0
	for _, row := range a.steps {
		if !row.OK {
			driverOpErrors++
		}
	}
	emit(map[string]any{
		"ok":     true,
		"answer": answer,
		"observations": map[string]any{
			"checks": rows, "saved": a.saved, "binding": binding,
			"driver_ops": len(a.steps), "driver_op_errors": driverOpErrors,
			"failure_class": "cdp_semantic",
		},
		"metrics": map[string]any{"cdp_call_count": a.opCalls, "cdp_error_count": a.opErrors, "ws_disconnect_count": 0},
	})
}

func errText(err error) any {
	if err == nil {
		return nil
	}
	return truncate(err.Error(), 500)
}
