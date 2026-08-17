// rod scenario adapter.
//
// Drives the engine under test with the pinned github.com/go-rod/rod Go
// package — the Go ecosystem's high-level, auto-waiting CDP
// driver (chromedp's main alternative). Speaks the abb_scenario_adapter/1
// contract (../PROTOCOL.md): payload JSON on stdin, framework_probe.js result
// contract on stdout, mandatory two-layer binding gate, shared op vocabulary.
//
// The adapter exercises rod's own high-level surface — Browser.Page,
// Page.Navigate, Element.Click / Input / Text — because the column measures
// whether rod's abstractions hold on each engine. ControlURL connects to the
// runner-verified browser websocket exactly as given and never launches a
// browser.
package main

import (
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

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/input"
	"github.com/go-rod/rod/lib/proto"
)

type payloadT struct {
	BrowserWS              string           `json:"browser_ws"`
	RemoteCDP              bool             `json:"remote_cdp"`
	ExpectedRemoteIdentity remoteIdentityT  `json:"expected_remote_identity"`
	CDPPort                int              `json:"cdp_port"`
	ExpectProduct          string           `json:"expect_product"`
	ExpectUA               string           `json:"expect_ua"`
	ExpectProductLive      string           `json:"expect_product_live"`
	TaskURL                string           `json:"task_url"`
	Steps                  []map[string]any `json:"steps"`
	Checks                 []map[string]any `json:"checks"`
	ConnectTimeoutMS       int              `json:"connect_timeout_ms"`
	ActionTimeoutMS        int              `json:"action_timeout_ms"`
	TaskTimeoutMS          int              `json:"task_timeout_ms"`
	ArtifactDir            string           `json:"artifact_dir"`
}

type remoteIdentityT struct {
	Product         string `json:"product"`
	ProtocolVersion string `json:"protocolVersion"`
	Revision        string `json:"revision"`
}

func remoteIdentityBinding(expected, actual remoteIdentityT) map[string]any {
	fields := []string{"product", "protocolVersion", "revision"}
	mismatches := []string{}
	if actual.Product != expected.Product {
		mismatches = append(mismatches, "product")
	}
	if actual.ProtocolVersion != expected.ProtocolVersion {
		mismatches = append(mismatches, "protocolVersion")
	}
	if actual.Revision != expected.Revision {
		mismatches = append(mismatches, "revision")
	}
	return map[string]any{
		"transport": "remote_cdp", "expected": expected, "actual": actual,
		"compared_fields": fields, "mismatches": mismatches,
		"verified": len(mismatches) == 0, "same_connection_as_task": true,
		"reconnect_allowed": false,
	}
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

type pageCreationResult struct {
	page *rod.Page
	err  error
}

type pageCreation struct {
	Attempt  int
	State    string
	TargetID string
	Error    string
	Page     *rod.Page
	Pending  <-chan pageCreationResult
}

var clientVersion = "v0.116.2" // kept in sync with go.mod

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
		return strconv.FormatFloat(v, 'f', -1, 64)
	default:
		data, err := json.Marshal(value)
		if err != nil {
			return fmt.Sprintf("%v", value)
		}
		return string(data)
	}
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

type adapter struct {
	payload         payloadT
	fixture         *url.URL
	trace           *os.File
	browser         *rod.Browser
	page            *rod.Page
	pages           []*rod.Page
	pageCreations   []*pageCreation
	saved           map[string]string
	steps           []stepResult
	opCalls         int
	opErrors        int
	budgetDeadline  time.Time
	closeTargetHook func(proto.TargetTargetID, time.Duration) (bool, error)
}

func (a *adapter) resolvePageCreation(creation *pageCreation, result pageCreationResult) error {
	creation.Pending = nil
	if result.err != nil {
		creation.Error = result.err.Error()
		// Browser.Page performs target creation, attachment and optional
		// navigation behind one error. Even a CDP error here may arrive after
		// creation, so it cannot prove that no target exists.
		creation.State = "ambiguous"
		return result.err
	}
	if result.page == nil || result.page.TargetID == "" {
		creation.State = "ambiguous"
		creation.Error = "Browser.Page returned no target id"
		return fmt.Errorf("%s", creation.Error)
	}
	creation.State = "created"
	creation.Page = result.page
	creation.TargetID = string(result.page.TargetID)
	a.pages = append(a.pages, result.page)
	a.page = result.page
	return nil
}

func (a *adapter) closeTargetForIsolation(
	targetID proto.TargetTargetID,
	timeout time.Duration,
) (success bool, err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			success = false
			err = fmt.Errorf("%v", recovered)
		}
	}()
	if a.closeTargetHook != nil {
		return a.closeTargetHook(targetID, timeout)
	}
	if a.browser == nil {
		return false, fmt.Errorf("rod root browser connection is unavailable")
	}
	timedBrowser := a.browser.Timeout(timeout)
	defer timedBrowser.CancelTimeout()
	result, err := (proto.TargetCloseTarget{TargetID: targetID}).Call(timedBrowser)
	if err != nil {
		return false, err
	}
	if result == nil {
		return false, fmt.Errorf("Target.closeTarget returned no result")
	}
	return result.Success, nil
}

func (a *adapter) cleanupPages() map[string]any {
	attempts := []map[string]any{}
	// A timed-out Browser.Page call may have completed after its caller returned.
	// Recover the page if its buffered result is now available so it can still
	// be closed on the original Rod connection.
	for _, creation := range a.pageCreations {
		if creation.Pending == nil {
			continue
		}
		select {
		case result := <-creation.Pending:
			_ = a.resolvePageCreation(creation, result)
		default:
		}
	}
	for index := len(a.pages) - 1; index >= 0; index-- {
		created := a.pages[index]
		creation := (*pageCreation)(nil)
		for _, candidate := range a.pageCreations {
			if candidate.Page == created {
				creation = candidate
				break
			}
		}
		closed := false
		for attempt := 1; attempt <= 2 && !closed; attempt++ {
			success, err := a.closeTargetForIsolation(
				created.TargetID,
				3*time.Second,
			)
			confirmed := err == nil && success
			timedOut := err != nil && strings.Contains(strings.ToLower(err.Error()), "deadline")
			entry := map[string]any{
				"target_id": string(created.TargetID),
				"attempt":   attempt,
				"success":   success,
				"confirmed": confirmed,
			}
			if err != nil {
				entry["error"] = err.Error()
			} else if !success {
				entry["error"] = "Target.closeTarget returned success=false"
			}
			if timedOut {
				entry["timed_out"] = true
			}
			attempts = append(attempts, entry)
			closed = confirmed
		}
		if creation != nil {
			if closed {
				creation.State = "closed"
			} else {
				creation.State = "cleanup_unconfirmed"
			}
		}
	}

	creationRows := make([]map[string]any, 0, len(a.pageCreations))
	confirmed := true
	for _, creation := range a.pageCreations {
		row := map[string]any{
			"attempt": creation.Attempt,
			"state":   creation.State,
		}
		if creation.TargetID != "" {
			row["target_id"] = creation.TargetID
		}
		if creation.Error != "" {
			row["error"] = creation.Error
		}
		creationRows = append(creationRows, row)
		if creation.State != "closed" && creation.State != "rejected" {
			confirmed = false
		}
	}
	return map[string]any{
		"backend":                 "Target.closeTarget via rod root connection",
		"required":                len(a.pageCreations) > 0,
		"confirmed":               confirmed,
		"same_connection_as_task": true,
		"creation_attempts":       creationRows,
		"attempts":                attempts,
	}
}

func applyCleanupContract(outcome, cleanup map[string]any, label string) map[string]any {
	observations, _ := outcome["observations"].(map[string]any)
	if observations == nil {
		observations = map[string]any{}
	}
	observations["target_cleanup"] = cleanup
	confirmed := cleanup["confirmed"] == true
	observations["isolation_restored"] = confirmed
	outcome["observations"] = observations
	if confirmed {
		return outcome
	}
	return map[string]any{
		"ok":     false,
		"status": "infra",
		"error": map[string]any{
			"class":   "script_error",
			"message": fmt.Sprintf("%s target cleanup was not confirmed: %v", label, cleanup),
		},
		"answer": outcome["answer"],
		"observations": map[string]any{
			"target_cleanup":     cleanup,
			"isolation_restored": false,
			"primary_outcome":    outcome,
		},
		"metrics": outcome["metrics"],
	}
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

// opTimeout clamps an op's wait to the remaining task budget so the adapter
// always emits a graded result instead of being killed mid-run.
func (a *adapter) opTimeout(timeoutMS int) (time.Duration, error) {
	remaining := time.Until(a.budgetDeadline)
	if remaining <= 0 {
		return 0, fmt.Errorf("task budget exhausted before op could run")
	}
	d := time.Duration(timeoutMS) * time.Millisecond
	if d > remaining {
		d = remaining
	}
	return d, nil
}

// call wraps one rod interaction: counts it, recovers rod's internal panics
// into errors, and normalizes context deadline errors.
func (a *adapter) call(fn func() error) (err error) {
	a.opCalls++
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("%v", r)
		}
		if err != nil {
			a.opErrors++
			if strings.Contains(err.Error(), "context deadline exceeded") {
				err = fmt.Errorf("timeout: %s", err.Error())
			}
		}
	}()
	return fn()
}

func (a *adapter) evalValue(timeoutMS int, expression string) (any, error) {
	d, err := a.opTimeout(timeoutMS)
	if err != nil {
		a.opCalls++
		a.opErrors++
		return nil, err
	}
	var value any
	err = a.call(func() error {
		// eval() of the raw program keeps completion-value semantics so
		// multi-statement expressions ("a; b; c") stay legal.
		quoted, _ := json.Marshal(expression)
		obj, evalErr := a.page.Timeout(d).Eval(fmt.Sprintf("() => eval(%s)", string(quoted)))
		if evalErr != nil {
			return evalErr
		}
		if obj != nil && obj.Value.Val() != nil {
			value = obj.Value.Val()
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return value, nil
}

func selExpr(sel, body string) string {
	quoted, _ := json.Marshal(sel)
	escaped := strings.ReplaceAll(sel, `"`, `\"`)
	return fmt.Sprintf(`(() => { const el = document.querySelector(%s); if (!el) throw new Error("no element matches %s"); %s })()`, quoted, escaped, body)
}

func (a *adapter) pollUntil(timeoutMS int, expression, what string) error {
	d, err := a.opTimeout(timeoutMS)
	if err != nil {
		return err
	}
	deadline := time.Now().Add(d)
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

func (a *adapter) element(timeoutMS int, sel string) (*rod.Element, error) {
	d, err := a.opTimeout(timeoutMS)
	if err != nil {
		return nil, err
	}
	var el *rod.Element
	err = a.call(func() error {
		found, elErr := a.page.Timeout(d).Element(sel)
		el = found
		return elErr
	})
	if err != nil {
		return nil, err
	}
	return el, nil
}

func (a *adapter) settleNavigation(timeoutMS int, target string) error {
	parsed, err := url.Parse(target)
	if err != nil {
		return err
	}
	query := ""
	if parsed.RawQuery != "" {
		query = "?" + parsed.RawQuery
	}
	wantPath, _ := json.Marshal(parsed.Path + query)
	return a.pollUntil(timeoutMS, fmt.Sprintf(`document.readyState === "complete" && (location.pathname + location.search) === %s`, wantPath), "navigation to "+target)
}

func (a *adapter) newPage(timeoutMS int) error {
	d, err := a.opTimeout(timeoutMS)
	if err != nil {
		return err
	}
	// rod gotcha: browser.Timeout(d).Page(...) + CancelTimeout() poisons the
	// page's context lineage — element ops later die with "context canceled".
	// Create the page with a clean context and race the timeout instead.
	creation := &pageCreation{
		Attempt: len(a.pageCreations) + 1,
		State:   "requested",
	}
	a.pageCreations = append(a.pageCreations, creation)
	return a.call(func() error {
		done := make(chan pageCreationResult, 1)
		creation.Pending = done
		go func() {
			page, pageErr := a.browser.Page(proto.TargetCreateTarget{URL: "about:blank"})
			done <- pageCreationResult{page: page, err: pageErr}
		}()
		select {
		case result := <-done:
			return a.resolvePageCreation(creation, result)
		case <-time.After(d):
			creation.State = "ambiguous"
			creation.Error = fmt.Sprintf("timeout after %dms creating page", timeoutMS)
			return fmt.Errorf("timeout after %dms creating page", timeoutMS)
		}
	})
}

func axValue(value *proto.AccessibilityAXValue) string {
	if value == nil || value.Value.Val() == nil {
		return ""
	}
	return fmt.Sprintf("%v", value.Value.Val())
}

func (a *adapter) fullAXTree() ([]*proto.AccessibilityAXNode, error) {
	var nodes []*proto.AccessibilityAXNode
	err := a.call(func() error {
		if err := (proto.AccessibilityEnable{}).Call(a.page); err != nil {
			return err
		}
		result, err := (proto.AccessibilityGetFullAXTree{}).Call(a.page)
		if err != nil {
			return err
		}
		nodes = result.Nodes
		return nil
	})
	return nodes, err
}

func findAXIdentity(nodes []*proto.AccessibilityAXNode, role, name string) (string, error) {
	for _, node := range nodes {
		// A null entry in the AX tree would panic here, and the adapter dying
		// is recorded as the engine failing the task.
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

func formatComputedStyle(step map[string]any, computed []*proto.CSSCSSComputedStyleProperty) string {
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
		err := a.call(func() error {
			ver, verErr := a.browser.Version()
			if verErr != nil {
				return verErr
			}
			product, userAgent = ver.Product, ver.UserAgent
			return nil
		})
		if err != nil {
			return nil, err
		}
		if op == "version" {
			return product, nil
		}
		return userAgent, nil
	case "new_page":
		if err := a.newPage(timeout); err != nil {
			return nil, err
		}
		return "page_created", nil
	}

	if a.page == nil {
		if err := a.newPage(timeout); err != nil {
			return nil, err
		}
	}

	switch op {
	case "goto":
		target := a.payload.TaskURL
		if u, ok := step["url"].(string); ok && u != "" {
			target = a.substitute(u)
		}
		d, err := a.opTimeout(timeout)
		if err != nil {
			return nil, err
		}
		if err := a.call(func() error { return a.page.Timeout(d).Navigate(target) }); err != nil {
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
		d, err := a.opTimeout(timeout)
		if err != nil {
			return nil, err
		}
		if err := a.call(func() error { return a.page.Timeout(d).Reload() }); err != nil {
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
		d, err := a.opTimeout(timeout)
		if err != nil {
			return nil, err
		}
		if err := a.call(func() error {
			if op == "go_back" {
				return a.page.Timeout(d).NavigateBack()
			}
			return a.page.Timeout(d).NavigateForward()
		}); err != nil {
			return nil, err
		}
		if err := a.pollUntil(timeout, fmt.Sprintf(`document.readyState === "complete" && window.__abb_nav_probe !== %q + "|" + location.href`, navNonce), op); err != nil {
			return nil, err
		}
		return "ok", nil
	case "click":
		times := 1
		if v, ok := step["times"].(float64); ok {
			times = int(v)
		}
		for i := 0; i < times; i++ {
			el, err := a.element(timeout, sel)
			if err != nil {
				return nil, err
			}
			if err := a.call(func() error { return el.Click(proto.InputMouseButtonLeft, 1) }); err != nil {
				return nil, err
			}
		}
		return fmt.Sprintf("clicked x%d", times), nil
	case "fill":
		value := ""
		if v, ok := step["value"].(string); ok {
			value = a.substitute(v)
		}
		el, err := a.element(timeout, sel)
		if err != nil {
			return nil, err
		}
		if err := a.call(func() error {
			if selErr := el.SelectAllText(); selErr != nil {
				return selErr
			}
			return el.Input(value)
		}); err != nil {
			return nil, err
		}
		return "filled", nil
	case "type":
		text := ""
		if v, ok := step["text"].(string); ok {
			text = a.substitute(v)
		}
		el, err := a.element(timeout, sel)
		if err != nil {
			return nil, err
		}
		if err := a.call(func() error { return el.Input(text) }); err != nil {
			return nil, err
		}
		return "typed", nil
	case "press":
		key, _ := step["key"].(string)
		mapped, ok := map[string]input.Key{
			"Enter": input.Enter, "Tab": input.Tab, "Escape": input.Escape, "Backspace": input.Backspace,
		}[key]
		if !ok {
			return nil, fmt.Errorf("unsupported key %q for press", key)
		}
		el, err := a.element(timeout, sel)
		if err != nil {
			return nil, err
		}
		if err := a.call(func() error {
			actions, kaErr := el.KeyActions()
			if kaErr != nil {
				return kaErr
			}
			return actions.Press(mapped).Do()
		}); err != nil {
			return nil, err
		}
		return "pressed " + key, nil
	case "check":
		already, err := a.evalValue(timeout, selExpr(sel, "return !!el.checked;"))
		if err != nil {
			return nil, err
		}
		if already != true {
			el, elErr := a.element(timeout, sel)
			if elErr != nil {
				return nil, elErr
			}
			if err := a.call(func() error { return el.Click(proto.InputMouseButtonLeft, 1) }); err != nil {
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
		el, err := a.element(timeout, sel)
		if err != nil {
			return nil, err
		}
		if err := a.call(func() error { return el.Focus() }); err != nil {
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
		return a.evalValue(timeout, selExpr(sel, "return el.textContent;"))
	case "inner_text":
		return a.evalValue(timeout, selExpr(sel, "return el.innerText;"))
	case "get_attribute":
		name, _ := step["name"].(string)
		quotedName, _ := json.Marshal(name)
		return a.evalValue(timeout, selExpr(sel, fmt.Sprintf("return el.getAttribute(%s);", quotedName)))
	case "input_value":
		el, err := a.element(timeout, sel)
		if err != nil {
			return nil, err
		}
		var out any
		err = a.call(func() error {
			prop, propErr := el.Property("value")
			if propErr != nil {
				return propErr
			}
			out = prop.Val()
			return nil
		})
		if err != nil {
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
		return a.fullAXTree()
	case "ax_node_identity":
		role, _ := step["role"].(string)
		name := a.substitute(fmt.Sprintf("%v", step["name"]))
		nodes, err := a.fullAXTree()
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
		var computed []*proto.CSSCSSComputedStyleProperty
		err := a.call(func() error {
			doc, err := (proto.DOMGetDocument{}).Call(a.page)
			if err != nil {
				return err
			}
			found, err := (proto.DOMQuerySelector{NodeID: doc.Root.NodeID, Selector: sel}).Call(a.page)
			if err != nil {
				return err
			}
			if found.NodeID == 0 {
				return fmt.Errorf("no element matches %s", sel)
			}
			if err := (proto.CSSEnable{}).Call(a.page); err != nil {
				return err
			}
			result, err := (proto.CSSGetComputedStyleForNode{NodeID: found.NodeID}).Call(a.page)
			if err != nil {
				return err
			}
			computed = result.ComputedStyle
			return nil
		})
		if err != nil {
			return nil, err
		}
		return formatComputedStyle(step, computed), nil
	case "title":
		return a.evalValue(timeout, "document.title")
	case "url":
		return a.evalValue(timeout, "location.href")
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
		return present && value == expected, fmt.Sprintf("%s=%s expected=%q", name, quoteOrNull(value, present), expected)
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
		"driver": "rod", "browser_ws": payload.BrowserWS,
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

	// ---- Connect + binding gate 2/2: live-transport identity.
	expectLive := payload.ExpectProductLive
	if expectLive == "" {
		expectLive = payload.ExpectProduct
	}
	browser := rod.New().ControlURL(payload.BrowserWS)
	var liveIdentity remoteIdentityT
	connectErr := a.call(func() error {
		done := make(chan error, 1)
		go func() {
			if err := browser.Connect(); err != nil {
				done <- err
				return
			}
			ver, verErr := browser.Version()
			if verErr != nil {
				done <- verErr
				return
			}
			liveIdentity = remoteIdentityT{Product: ver.Product, ProtocolVersion: ver.ProtocolVersion, Revision: ver.Revision}
			done <- nil
		}()
		select {
		case err := <-done:
			return err
		case <-time.After(time.Duration(payload.ConnectTimeoutMS) * time.Millisecond):
			return fmt.Errorf("connect timeout after %dms", payload.ConnectTimeoutMS)
		}
	})
	liveProduct := liveIdentity.Product
	a.traceLine(map[string]any{"direction": "rod", "step": "connect", "ok": connectErr == nil, "error": errText(connectErr)})
	if connectErr != nil {
		// A refused/failed connect is a genuine compatibility result: the
		// engine cannot be driven by this client. Grade every check as failed.
		rows := []checkRow{{Name: "driver_connect", Status: "fail", Evidence: fmt.Sprintf("rod@%s could not drive %s: %s", clientVersion, payload.BrowserWS, truncate(connectErr.Error(), 500))}}
		for idx, check := range payload.Checks {
			rows = append(rows, checkRow{Name: checkName(check, idx), Status: "fail", Evidence: "client did not connect; scenario not executed"})
		}
		outcome := map[string]any{
			"ok":     true,
			"answer": fmt.Sprintf("0/%d checks", len(rows)),
			"observations": map[string]any{
				"checks": rows, "saved": map[string]string{}, "binding": binding,
				"connect_error": truncate(connectErr.Error(), 500), "failure_class": "cdp_semantic",
			},
			"metrics": map[string]any{"cdp_call_count": 1, "cdp_error_count": 1, "ws_disconnect_count": 0},
		}
		emit(applyCleanupContract(outcome, a.cleanupPages(), "rod"))
		return
	}
	a.browser = browser
	binding["expect_product_live"] = expectLive
	binding["live_product"] = liveProduct
	binding["live_check"] = "rod_browser_version"
	identityVerified := liveProduct == expectLive
	if payload.RemoteCDP {
		for key, value := range remoteIdentityBinding(payload.ExpectedRemoteIdentity, liveIdentity) {
			binding[key] = value
		}
		identityVerified = binding["verified"] == true
	}
	if !identityVerified {
		outcome := map[string]any{
			"ok": false,
			"error": map[string]any{
				"class":   "script_error",
				"message": fmt.Sprintf("binding gate: live rod transport identity does not match the expected engine (product=%q protocolVersion=%q revision=%q)", liveIdentity.Product, liveIdentity.ProtocolVersion, liveIdentity.Revision),
			},
			"observations": map[string]any{"binding": binding},
			"metrics":      map[string]any{"cdp_call_count": a.opCalls, "cdp_error_count": a.opErrors, "ws_disconnect_count": 0},
		}
		emit(applyCleanupContract(outcome, a.cleanupPages(), "rod"))
		return
	}
	binding["verified"] = true
	a.traceLine(map[string]any{"direction": "rod", "step": "binding_verified", "identity": liveIdentity})

	// ---- Scenario steps.
	for idx, step := range payload.Steps {
		value, err := a.runOp(step)
		result := stepResult{OK: err == nil, Value: value}
		if err != nil {
			result.Err = truncate(err.Error(), 1000)
		}
		a.steps = append(a.steps, result)
		a.traceLine(map[string]any{"direction": "rod", "step": idx, "op": step["op"], "selector": step["selector"], "ok": result.OK, "error": result.Err})
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

	rows := []checkRow{{Name: "driver_connect", Status: "pass", Evidence: fmt.Sprintf("rod@%s bound to %s", clientVersion, liveProduct)}}
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
	outcome := map[string]any{
		"ok":     true,
		"answer": answer,
		"observations": map[string]any{
			"checks": rows, "saved": a.saved, "binding": binding,
			"driver_ops": len(a.steps), "driver_op_errors": driverOpErrors,
			"failure_class": "cdp_semantic",
		},
		"metrics": map[string]any{"cdp_call_count": a.opCalls, "cdp_error_count": a.opErrors, "ws_disconnect_count": 0},
	}
	emit(applyCleanupContract(outcome, a.cleanupPages(), "rod"))
}

func errText(err error) any {
	if err == nil {
		return nil
	}
	return truncate(err.Error(), 500)
}
