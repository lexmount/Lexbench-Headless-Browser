// chromiumoxide scenario adapter.
//
// Drives the engine under test with the pinned `chromiumoxide` crate — the Rust
// ecosystem's async CDP driver. Speaks the
// abb_scenario_adapter/1 contract (../../PROTOCOL.md): payload JSON on stdin,
// framework_probe.js result contract on stdout, mandatory two-layer binding
// gate, shared op vocabulary.
//
// The adapter exercises chromiumoxide's own surface — Browser::connect,
// Browser::new_page, Page::goto/evaluate, Element::click/type_str — because
// the column measures whether those abstractions hold on each engine.
// Browser::connect attaches to the runner-verified browser websocket exactly
// as given and never launches a browser. Per-op waits are enforced with
// tokio timeouts and clamped to the runner's task budget so every attempt
// emits a graded result.

use std::collections::HashMap;
use std::io::Read;
use std::time::{Duration, Instant};

use chromiumoxide::browser::Browser;
use chromiumoxide::cdp::browser_protocol::{accessibility, css, dom};
use chromiumoxide::page::Page;
use futures::StreamExt;
use serde_json::{json, Value};

const CLIENT_VERSION: &str = "0.9.1"; // kept in sync with Cargo.toml

fn emit(obj: Value) {
    print!("{}", obj);
}

fn emit_infra(message: &str, binding: &Value, calls: u64, errs: u64) {
    emit(json!({
        "ok": false,
        "error": {"class": "script_error", "message": message},
        "observations": {"binding": binding},
        "metrics": {"cdp_call_count": calls, "cdp_error_count": errs, "ws_disconnect_count": 0},
    }));
}

fn to_saved_string(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::String(s) => s.clone(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

fn truncate(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn http_json(url: &str) -> Result<Value, String> {
    // std-only HTTP GET via curl keeps the dependency tree small; curl is a
    // hard host requirement of the harness already.
    let out = std::process::Command::new("curl")
        .args(["-s", "--max-time", "4", url])
        .output()
        .map_err(|e| e.to_string())?;
    serde_json::from_slice(&out.stdout).map_err(|e| format!("invalid JSON from {url}: {e}"))
}

fn ax_value(value: Option<&accessibility::AxValue>) -> String {
    match value.and_then(|v| v.value.as_ref()) {
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
        None => String::new(),
    }
}

fn find_ax_identity(nodes: &[accessibility::AxNode], role: &str, name: &str) -> Result<String, String> {
    let node = nodes
        .iter()
        .find(|node| ax_value(node.role.as_ref()) == role && ax_value(node.name.as_ref()) == name)
        .ok_or_else(|| format!("AX node role={role:?} name={name:?} not found"))?;
    let backend_id = node
        .backend_dom_node_id
        .as_ref()
        .ok_or_else(|| format!("AX node role={role:?} name={name:?} has no backendDOMNodeId"))?;
    Ok(format!(
        "role={role}|name={name}|backendDOMNodeId={}",
        backend_id.inner()
    ))
}

fn format_computed_style(step: &Value, computed: &[css::CssComputedStyleProperty]) -> String {
    let required: Vec<String> = step["required_properties"]
        .as_array()
        .filter(|values| !values.is_empty())
        .map(|values| values.iter().map(to_saved_string).collect())
        .unwrap_or_else(|| {
            ["display", "visibility", "opacity", "pointer-events"]
                .iter()
                .map(|name| (*name).to_string())
                .collect()
        });
    let minimum = step["min_property_count"].as_u64().unwrap_or(100) as usize;
    let properties: HashMap<&str, &str> = computed
        .iter()
        .map(|entry| (entry.name.as_str(), entry.value.as_str()))
        .collect();
    let readable = required
        .iter()
        .all(|name| properties.get(name.as_str()).is_some_and(|value| !value.is_empty()));
    let prefix = if properties.len() >= minimum && readable {
        "breadth-ok"
    } else {
        "breadth-insufficient"
    };
    let details = required
        .iter()
        .map(|name| {
            format!(
                "{name}={}",
                properties.get(name.as_str()).copied().unwrap_or("<missing>")
            )
        })
        .collect::<Vec<_>>()
        .join("|");
    format!("{prefix}|count={}|{details}", properties.len())
}

struct Adapter {
    task_url: String,
    fixture_origin: String,
    fixture_host: String,
    artifact_dir: String,
    action_timeout_ms: u64,
    budget_deadline: Instant,
    browser: Option<Browser>,
    page: Option<Page>,
    saved: HashMap<String, String>,
    steps: Vec<(bool, Option<Value>, String)>, // (ok, value, err)
    op_calls: u64,
    op_errors: u64,
    trace_path: std::path::PathBuf,
}

impl Adapter {
    fn trace(&self, obj: Value) {
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&self.trace_path) {
            let _ = writeln!(f, "{}", obj);
        }
    }

    fn substitute(&self, raw: &str) -> String {
        raw.replace("{fixture_url}", &self.task_url)
            .replace("{fixture_origin}", &self.fixture_origin)
            .replace("{fixture_host}", &self.fixture_host)
            .replace("{artifact_dir}", &self.artifact_dir)
    }

    fn op_timeout(&self, timeout_ms: u64) -> Result<Duration, String> {
        let remaining = self.budget_deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err("task budget exhausted before op could run".to_string());
        }
        Ok(Duration::from_millis(timeout_ms).min(remaining))
    }

    async fn eval_value(&mut self, timeout_ms: u64, expression: &str) -> Result<Value, String> {
        self.op_calls += 1;
        let d = match self.op_timeout(timeout_ms) {
            Ok(d) => d,
            Err(e) => {
                self.op_errors += 1;
                return Err(e);
            }
        };
        let page = self.page.as_ref().ok_or("no page")?;
        // eval() of the raw program keeps Runtime.evaluate's completion-value
        // semantics: multi-statement expressions ("a; b; c") are legal and the
        // last statement's value is returned, matching every other adapter.
        let quoted = serde_json::to_string(expression).map_err(|e| e.to_string())?;
        let fut = page.evaluate(format!("(() => eval({quoted}))()"));
        match tokio::time::timeout(d, fut).await {
            Err(_) => {
                self.op_errors += 1;
                Err(format!("timeout after {}ms", d.as_millis()))
            }
            Ok(Err(e)) => {
                self.op_errors += 1;
                Err(truncate(&e.to_string(), 500))
            }
            Ok(Ok(result)) => Ok(result.value().cloned().unwrap_or(Value::Null)),
        }
    }

    async fn poll_until(&mut self, timeout_ms: u64, expression: &str, what: &str) -> Result<(), String> {
        let d = self.op_timeout(timeout_ms)?;
        let deadline = Instant::now() + d;
        loop {
            if let Ok(value) = self.eval_value(2000, expression).await {
                let truthy = match &value {
                    Value::Bool(b) => *b,
                    Value::Null => false,
                    Value::String(s) => !s.is_empty(),
                    Value::Number(n) => n.as_f64().unwrap_or(0.0) != 0.0,
                    _ => true,
                };
                if truthy {
                    return Ok(());
                }
            }
            if Instant::now() > deadline {
                return Err(format!("timeout after {timeout_ms}ms waiting for {what}"));
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    async fn settle_navigation(&mut self, timeout_ms: u64, target: &str) -> Result<(), String> {
        let path = target.splitn(2, "://").nth(1).and_then(|rest| rest.find('/').map(|i| &rest[i..])).unwrap_or("/");
        let path_no_hash = path.split('#').next().unwrap_or(path);
        let want = serde_json::to_string(path_no_hash).unwrap();
        self.poll_until(
            timeout_ms,
            &format!(r#"document.readyState === "complete" && (location.pathname + location.search) === {want}"#),
            &format!("navigation to {target}"),
        )
        .await
    }

    fn sel_expr(sel: &str, body: &str) -> String {
        let quoted = serde_json::to_string(sel).unwrap();
        let escaped = sel.replace('"', "\\\"");
        format!(r#"(() => {{ const el = document.querySelector({quoted}); if (!el) throw new Error("no element matches {escaped}"); {body} }})()"#)
    }

    async fn with_timeout<T, F>(&mut self, timeout_ms: u64, fut: F) -> Result<T, String>
    where
        F: std::future::Future<Output = Result<T, chromiumoxide::error::CdpError>>,
    {
        self.op_calls += 1;
        let d = match self.op_timeout(timeout_ms) {
            Ok(d) => d,
            Err(e) => {
                self.op_errors += 1;
                return Err(e);
            }
        };
        match tokio::time::timeout(d, fut).await {
            Err(_) => {
                self.op_errors += 1;
                Err(format!("timeout after {}ms", d.as_millis()))
            }
            Ok(Err(e)) => {
                self.op_errors += 1;
                Err(truncate(&e.to_string(), 500))
            }
            Ok(Ok(v)) => Ok(v),
        }
    }

    async fn click_selector(&mut self, timeout_ms: u64, sel: &str) -> Result<(), String> {
        let page = self.page.as_ref().ok_or("no page")?.clone();
        let sel_owned = sel.to_string();
        let element = self
            .with_timeout(timeout_ms, async { page.find_element(sel_owned).await })
            .await?;
        self.with_timeout(timeout_ms, async { element.click().await }).await?;
        Ok(())
    }

    async fn full_ax_tree(&mut self, timeout_ms: u64) -> Result<Vec<accessibility::AxNode>, String> {
        let page = self.page.as_ref().ok_or("no page")?.clone();
        self.with_timeout(timeout_ms, async {
            page.execute(accessibility::EnableParams::default()).await
        })
        .await?;
        let page = self.page.as_ref().ok_or("no page")?.clone();
        let response = self
            .with_timeout(timeout_ms, async {
                page.execute(accessibility::GetFullAxTreeParams::default())
                    .await
            })
            .await?;
        Ok(response.result.nodes)
    }

    async fn run_op(&mut self, step: &Value) -> Result<Value, String> {
        let op = step["op"].as_str().unwrap_or("");
        let sel = step["selector"].as_str().map(|s| self.substitute(s));
        let timeout = step["timeout_ms"].as_u64().unwrap_or(self.action_timeout_ms);

        match op {
            "wait_ms" => {
                tokio::time::sleep(Duration::from_millis(step["ms"].as_u64().unwrap_or(100))).await;
                Ok(Value::Null)
            }
            "version" | "user_agent" => {
                let browser = self.browser.as_mut().ok_or("no browser")?;
                let ver = browser.version().await.map_err(|e| truncate(&e.to_string(), 500))?;
                self.op_calls += 1;
                if op == "version" {
                    Ok(Value::String(ver.product))
                } else {
                    Ok(Value::String(ver.user_agent))
                }
            }
            "new_page" => {
                let browser = self.browser.as_mut().ok_or("no browser")?;
                let d = self.budget_deadline.saturating_duration_since(Instant::now());
                if d.is_zero() {
                    return Err("task budget exhausted before op could run".to_string());
                }
                self.op_calls += 1;
                match tokio::time::timeout(Duration::from_millis(timeout).min(d), browser.new_page("about:blank")).await {
                    Err(_) => {
                        self.op_errors += 1;
                        Err(format!("timeout after {timeout}ms creating page"))
                    }
                    Ok(Err(e)) => {
                        self.op_errors += 1;
                        Err(truncate(&e.to_string(), 500))
                    }
                    Ok(Ok(page)) => {
                        self.page = Some(page);
                        Ok(json!("page_created"))
                    }
                }
            }
            "goto" => {
                let target = step["url"].as_str().map(|u| self.substitute(u)).unwrap_or_else(|| self.task_url.clone());
                let page = self.page.as_ref().ok_or("no page (run new_page first)")?.clone();
                let target_clone = target.clone();
                self.with_timeout(timeout, async { page.goto(target_clone).await }).await?;
                self.settle_navigation(timeout, &target).await?;
                Ok(json!("navigated"))
            }
            "reload" => {
                self.eval_value(timeout, r#"window.__abb_reload_probe = 1, "marked""#).await?;
                let page = self.page.as_ref().ok_or("no page")?.clone();
                self.with_timeout(timeout, async { page.reload().await }).await?;
                self.poll_until(timeout, r#"document.readyState === "complete" && !window.__abb_reload_probe"#, "reload to settle").await?;
                Ok(json!("reloaded"))
            }
            "go_back" | "go_forward" => {
                let nav_nonce = format!("np{}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0));
                self.eval_value(timeout, &format!(r#"window.__abb_nav_probe = "{nav_nonce}|" + location.href, "marked""#)).await?;
                let fn_call = if op == "go_back" { "history.back()" } else { "history.forward()" };
                self.eval_value(timeout, &format!("{fn_call}, 'initiated'")).await?;
                self.poll_until(timeout, &format!(r#"document.readyState === "complete" && window.__abb_nav_probe !== "{nav_nonce}|" + location.href"#), op).await?;
                Ok(json!("ok"))
            }
            "click" => {
                let sel = sel.ok_or("click requires selector")?;
                let times = step["times"].as_u64().unwrap_or(1);
                for _ in 0..times {
                    self.click_selector(timeout, &sel).await?;
                }
                Ok(json!(format!("clicked x{times}")))
            }
            "fill" => {
                let sel = sel.ok_or("fill requires selector")?;
                let value = step["value"].as_str().map(|v| self.substitute(v)).unwrap_or_default();
                // Focus + select-all so type_str replaces the existing value.
                self.eval_value(timeout, &Self::sel_expr(&sel, r#"el.focus(); if (typeof el.select === "function") el.select(); return "focused";"#)).await?;
                let page = self.page.as_ref().ok_or("no page")?.clone();
                let sel_owned = sel.clone();
                let element = self.with_timeout(timeout, async { page.find_element(sel_owned).await }).await?;
                self.with_timeout(timeout, async { element.type_str(value).await }).await?;
                Ok(json!("filled"))
            }
            "type" => {
                let sel = sel.ok_or("type requires selector")?;
                let text = step["text"].as_str().map(|v| self.substitute(v)).unwrap_or_default();
                let page = self.page.as_ref().ok_or("no page")?.clone();
                let sel_owned = sel.clone();
                let element = self.with_timeout(timeout, async { page.find_element(sel_owned).await }).await?;
                self.with_timeout(timeout, async { element.type_str(text).await }).await?;
                Ok(json!("typed"))
            }
            "press" => {
                let sel = sel.ok_or("press requires selector for the chromiumoxide adapter")?;
                let key = step["key"].as_str().unwrap_or("");
                if key != "Enter" {
                    return Err(format!("unsupported key {key:?} for press"));
                }
                // press_key dispatches raw key events without focusing the
                // element first; focus explicitly so the key lands on target.
                self.eval_value(timeout, &Self::sel_expr(&sel, "el.focus(); return 'focused';")).await?;
                let page = self.page.as_ref().ok_or("no page")?.clone();
                let sel_owned = sel.clone();
                let element = self.with_timeout(timeout, async { page.find_element(sel_owned).await }).await?;
                self.with_timeout(timeout, async { element.press_key("Enter").await }).await?;
                Ok(json!("pressed Enter"))
            }
            "check" => {
                let sel = sel.ok_or("check requires selector")?;
                let already = self.eval_value(timeout, &Self::sel_expr(&sel, "return !!el.checked;")).await?;
                if already != Value::Bool(true) {
                    self.click_selector(timeout, &sel).await?;
                }
                Ok(json!("checked"))
            }
            "select_option" => {
                let sel = sel.ok_or("select_option requires selector")?;
                let value = step["value"].as_str().map(|v| self.substitute(v)).unwrap_or_default();
                let quoted = serde_json::to_string(&value).unwrap();
                self.eval_value(timeout, &Self::sel_expr(&sel, &format!(
                    r#"el.value = {quoted}; el.dispatchEvent(new Event("input", {{bubbles: true}})); el.dispatchEvent(new Event("change", {{bubbles: true}})); return [el.value];"#
                ))).await
            }
            "focus" => {
                let sel = sel.ok_or("focus requires selector")?;
                self.eval_value(timeout, &Self::sel_expr(&sel, r#"el.focus(); return "focused";"#)).await?;
                Ok(json!("focused"))
            }
            "evaluate" => {
                let expr = self.substitute(step["expression"].as_str().unwrap_or(""));
                self.eval_value(timeout, &expr).await
            }
            "wait_for_function" => {
                let expr = self.substitute(step["expression"].as_str().unwrap_or(""));
                self.poll_until(timeout, &expr, "predicate").await?;
                Ok(json!("predicate_true"))
            }
            "wait_for_selector" => {
                let sel = sel.ok_or("wait_for_selector requires selector")?;
                let quoted = serde_json::to_string(&sel).unwrap();
                let expression = match step["state"].as_str() {
                    Some("hidden") | Some("detached") => format!(
                        r#"(() => {{ const el = document.querySelector({quoted}); if (!el) return true; const s = window.getComputedStyle(el); return s.display === "none" || s.visibility === "hidden"; }})()"#
                    ),
                    Some("visible") => format!(
                        r#"(() => {{ const el = document.querySelector({quoted}); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; }})()"#
                    ),
                    _ => format!("!!document.querySelector({quoted})"),
                };
                self.poll_until(timeout, &expression, &format!("selector {sel}")).await?;
                Ok(json!("selector_ready"))
            }
            "text_content" => {
                let sel = sel.ok_or("text_content requires selector")?;
                self.eval_value(timeout, &Self::sel_expr(&sel, "return el.textContent;")).await
            }
            "inner_text" => {
                let sel = sel.ok_or("inner_text requires selector")?;
                self.eval_value(timeout, &Self::sel_expr(&sel, "return el.innerText;")).await
            }
            "get_attribute" => {
                let sel = sel.ok_or("get_attribute requires selector")?;
                let name = serde_json::to_string(step["name"].as_str().unwrap_or("")).unwrap();
                self.eval_value(timeout, &Self::sel_expr(&sel, &format!("return el.getAttribute({name});"))).await
            }
            "input_value" => {
                let sel = sel.ok_or("input_value requires selector")?;
                self.eval_value(timeout, &Self::sel_expr(&sel, "return el.value;")).await
            }
            "count" => {
                let sel = sel.ok_or("count requires selector")?;
                let quoted = serde_json::to_string(&sel).unwrap();
                self.eval_value(timeout, &format!("document.querySelectorAll({quoted}).length")).await
            }
            "is_visible" => {
                let sel = sel.ok_or("is_visible requires selector")?;
                let quoted = serde_json::to_string(&sel).unwrap();
                self.eval_value(timeout, &format!(
                    r#"(() => {{ const el = document.querySelector({quoted}); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; }})()"#
                )).await
            }
            "is_checked" => {
                let sel = sel.ok_or("is_checked requires selector")?;
                self.eval_value(timeout, &Self::sel_expr(&sel, "return !!el.checked;")).await
            }
            "is_enabled" => {
                let sel = sel.ok_or("is_enabled requires selector")?;
                self.eval_value(timeout, &Self::sel_expr(&sel, "return !el.disabled;")).await
            }
            "ax_snapshot" => {
                let nodes = self.full_ax_tree(timeout).await?;
                serde_json::to_value(nodes).map_err(|e| e.to_string())
            }
            "ax_node_identity" => {
                let role = step["role"].as_str().unwrap_or("");
                let name = self.substitute(step["name"].as_str().unwrap_or(""));
                let nodes = self.full_ax_tree(timeout).await?;
                let identity = find_ax_identity(&nodes, role, &name)?;
                if let Some(compare_to) = step["compare_to"].as_str().filter(|value| !value.is_empty()) {
                    let before = self.saved.get(compare_to).map(String::as_str).unwrap_or("");
                    if before == identity {
                        Ok(json!(format!("stable|{identity}")))
                    } else {
                        Ok(json!(format!("changed|before={before}|after={identity}")))
                    }
                } else {
                    Ok(json!(identity))
                }
            }
            "computed_style_breadth" => {
                let sel = sel.ok_or("computed_style_breadth requires selector")?;
                let page = self.page.as_ref().ok_or("no page")?.clone();
                let document = self
                    .with_timeout(timeout, async {
                        page.execute(dom::GetDocumentParams::builder().depth(0).build())
                            .await
                    })
                    .await?;
                let page = self.page.as_ref().ok_or("no page")?.clone();
                let found = self
                    .with_timeout(timeout, async {
                        page.execute(dom::QuerySelectorParams::new(document.root.node_id, sel))
                            .await
                    })
                    .await?;
                if *found.node_id.inner() == 0 {
                    return Err("computed_style_breadth selector matched no element".to_string());
                }
                let page = self.page.as_ref().ok_or("no page")?.clone();
                self.with_timeout(timeout, async {
                    page.execute(css::EnableParams::default()).await
                })
                .await?;
                let page = self.page.as_ref().ok_or("no page")?.clone();
                let response = self
                    .with_timeout(timeout, async {
                        page.execute(css::GetComputedStyleForNodeParams::new(found.node_id))
                            .await
                    })
                    .await?;
                Ok(json!(format_computed_style(step, &response.computed_style)))
            }
            "title" => self.eval_value(timeout, "document.title").await,
            "url" => self.eval_value(timeout, "location.href").await,
            other => Err(format!("unknown op {other:?}")),
        }
    }

    fn evaluate_check(&self, check: &Value) -> (bool, String) {
        let kind = check["kind"].as_str().unwrap_or("");
        let name = check["name"].as_str().unwrap_or("");
        let saved = |n: &str| self.saved.get(n);
        match kind {
            "saved_equals" => {
                let value = saved(name);
                let expected = to_saved_string(&check["expected"]);
                let ok = value.map(|v| v == &expected).unwrap_or(false);
                (ok, format!("{name}={} expected={expected:?}", value.map(|v| format!("{v:?}")).unwrap_or("null".into())))
            }
            "saved_contains" | "saved_not_contains" => {
                let value = saved(name);
                let want = self.substitute(&to_saved_string(&check["expected"]));
                let contains = value.map(|v| v.contains(&want)).unwrap_or(false);
                let ok = if kind == "saved_contains" { contains } else { value.is_some() && !contains };
                let clause = if kind == "saved_contains" { "must contain" } else { "must NOT contain" };
                (ok, format!("{name}={} {clause} {want:?}", value.map(|v| format!("{:?}", truncate(v, 300))).unwrap_or("null".into())))
            }
            "saved_truthy" => {
                let value = saved(name);
                let truthy = value
                    .map(|v| !v.is_empty() && v != "undefined" && v != "null" && v != "false" && !v.starts_with("ERROR:"))
                    .unwrap_or(false);
                (truthy, format!("{name}={}", value.map(|v| format!("{:?}", truncate(v, 300))).unwrap_or("null".into())))
            }
            "step_ok" | "step_fails" => {
                let idx = check["step"].as_u64().unwrap_or(u64::MAX) as usize;
                let row = self.steps.get(idx);
                let ok_flag = row.map(|r| r.0).unwrap_or(false);
                let err_text = row.map(|r| if r.2.is_empty() { "none".to_string() } else { r.2.clone() }).unwrap_or("none".into());
                if kind == "step_ok" {
                    (ok_flag, format!("step {idx} ok={ok_flag} error={err_text}"))
                } else {
                    (row.map(|r| !r.0).unwrap_or(false), format!("step {idx} ok={ok_flag} (must fail) error={err_text}"))
                }
            }
            "file_nonempty" => {
                let path = self.substitute(check["path"].as_str().unwrap_or(""));
                let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
                (size > 0, format!("{path} size={size}"))
            }
            "any_of" => {
                let empty = vec![];
                let subs = check["checks"].as_array().unwrap_or(&empty);
                let results: Vec<(bool, String)> = subs.iter().map(|sub| self.evaluate_check(sub)).collect();
                let any_ok = results.iter().any(|(ok, _)| *ok);
                let evidence = results
                    .iter()
                    .map(|(ok, ev)| format!("{}: {ev}", if *ok { "pass" } else { "fail" }))
                    .collect::<Vec<_>>()
                    .join(" | ");
                (any_ok, evidence)
            }
            other => (false, format!("unknown check kind {other}")),
        }
    }
}

fn check_name(check: &Value, idx: usize) -> String {
    check["label"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or_else(|| check["kind"].as_str().filter(|s| !s.is_empty()))
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("check{idx}"))
}

#[tokio::main]
async fn main() {
    let mut stdin = String::new();
    if std::io::stdin().read_to_string(&mut stdin).is_err() {
        emit_infra("cannot read stdin", &Value::Null, 0, 0);
        return;
    }
    let payload: Value = match serde_json::from_str(&stdin) {
        Ok(v) => v,
        Err(e) => {
            emit_infra(&format!("invalid payload JSON on stdin: {e}"), &Value::Null, 0, 0);
            return;
        }
    };
    let browser_ws = payload["browser_ws"].as_str().unwrap_or("").to_string();
    let cdp_port = payload["cdp_port"].as_u64().unwrap_or(0);
    let expect_product = payload["expect_product"].as_str().unwrap_or("").to_string();
    let expect_ua = payload["expect_ua"].as_str().unwrap_or("").to_string();
    let expect_live = {
        let v = payload["expect_product_live"].as_str().unwrap_or("");
        if v.is_empty() { expect_product.clone() } else { v.to_string() }
    };
    let task_url = payload["task_url"].as_str().unwrap_or("").to_string();
    let artifact_dir = payload["artifact_dir"].as_str().unwrap_or(".").to_string();
    let connect_timeout_ms = payload["connect_timeout_ms"].as_u64().unwrap_or(15000);
    let action_timeout_ms = payload["action_timeout_ms"].as_u64().unwrap_or(8000);
    let task_timeout_ms = payload["task_timeout_ms"].as_u64().unwrap_or(30000);

    let mut binding = json!({
        "driver": "chromiumoxide", "browser_ws": browser_ws,
        "expect_product": expect_product, "verified": false, "gate": null,
        "client_version": CLIENT_VERSION,
    });
    if browser_ws.is_empty() || task_url.is_empty() {
        emit_infra("payload requires browser_ws and task_url", &binding, 0, 0);
        return;
    }
    let _ = std::fs::create_dir_all(&artifact_dir);

    // ---- Binding gate 1/2: HTTP identity of the endpoint, verbatim.
    if cdp_port == 0 || expect_product.is_empty() {
        emit_infra("binding gate: cdp_port and expect_product are required (refusing to run unverified)", &binding, 0, 0);
        return;
    }
    let version_info = match http_json(&format!("http://127.0.0.1:{cdp_port}/json/version")) {
        Ok(v) => v,
        Err(e) => {
            emit_infra(&format!("binding gate: /json/version unreachable on port {cdp_port}: {e}"), &binding, 0, 0);
            return;
        }
    };
    let http_product = version_info["Browser"].as_str().or(version_info["Product"].as_str()).unwrap_or("").to_string();
    let http_ua = version_info["User-Agent"].as_str().unwrap_or("").to_string();
    binding["http_product"] = json!(http_product);
    if http_product != expect_product || (!expect_ua.is_empty() && http_ua != expect_ua) {
        emit_infra(
            &format!("binding gate: endpoint on port {cdp_port} reports product={http_product:?} ua={http_ua:?}; expected product={expect_product:?} — refusing to run against an unverified engine"),
            &binding, 0, 0,
        );
        return;
    }
    if let Some(ws_from_version) = version_info["webSocketDebuggerUrl"].as_str() {
        if !ws_from_version.is_empty() && ws_from_version != browser_ws {
            emit_infra(&format!("binding gate: browser_ws {browser_ws} != verified endpoint {ws_from_version}"), &binding, 0, 0);
            return;
        }
    }
    binding["gate"] = json!("http_json_version");

    let fixture_origin = task_url.splitn(4, '/').take(3).collect::<Vec<_>>().join("/");
    let fixture_host = fixture_origin.splitn(2, "://").nth(1).unwrap_or("").to_string();
    let mut adapter = Adapter {
        task_url: task_url.clone(),
        fixture_origin,
        fixture_host,
        artifact_dir: artifact_dir.clone(),
        action_timeout_ms,
        budget_deadline: Instant::now() + Duration::from_millis(task_timeout_ms.saturating_sub(3000)),
        browser: None,
        page: None,
        saved: HashMap::new(),
        steps: Vec::new(),
        op_calls: 0,
        op_errors: 0,
        trace_path: std::path::Path::new(&artifact_dir).join("cdp.jsonl"),
    };
    let _ = std::fs::write(&adapter.trace_path, "");

    // ---- Connect + binding gate 2/2: live-transport identity.
    let connect_result = tokio::time::timeout(Duration::from_millis(connect_timeout_ms), async {
        let (browser, mut handler) = Browser::connect(browser_ws.clone()).await?;
        tokio::spawn(async move { while handler.next().await.is_some() {} });
        let ver = browser.version().await?;
        Ok::<(Browser, String), chromiumoxide::error::CdpError>((browser, ver.product))
    })
    .await;
    let (browser, live_product) = match connect_result {
        Err(_) | Ok(Err(_)) => {
            let connect_error = match connect_result {
                Err(_) => format!("connect timeout after {connect_timeout_ms}ms"),
                Ok(Err(e)) => truncate(&e.to_string(), 500),
                _ => unreachable!(),
            };
            adapter.trace(json!({"direction": "chromiumoxide", "step": "connect", "ok": false, "error": connect_error}));
            // A refused/failed connect is a genuine compatibility result.
            let empty = vec![];
            let checks = payload["checks"].as_array().unwrap_or(&empty);
            let mut rows = vec![json!({
                "name": "driver_connect", "status": "fail",
                "evidence": format!("chromiumoxide@{CLIENT_VERSION} could not drive {browser_ws}: {connect_error}"),
            })];
            for (idx, check) in checks.iter().enumerate() {
                rows.push(json!({"name": check_name(check, idx), "status": "fail", "evidence": "client did not connect; scenario not executed"}));
            }
            emit(json!({
                "ok": true,
                "answer": format!("0/{} checks", rows.len()),
                "observations": {"checks": rows, "saved": {}, "binding": binding, "connect_error": connect_error, "failure_class": "cdp_semantic"},
                "metrics": {"cdp_call_count": 1, "cdp_error_count": 1, "ws_disconnect_count": 0},
            }));
            return;
        }
        Ok(Ok(pair)) => pair,
    };
    adapter.trace(json!({"direction": "chromiumoxide", "step": "connect", "ok": true}));
    binding["expect_product_live"] = json!(expect_live);
    binding["live_product"] = json!(live_product);
    binding["live_check"] = json!("chromiumoxide_browser_version");
    if live_product != expect_live {
        emit_infra(
            &format!("binding gate: live chromiumoxide transport reports product={live_product:?}; expected {expect_live:?} — the client is not bound to the engine under test"),
            &binding, 1, 1,
        );
        return;
    }
    binding["verified"] = json!(true);
    adapter.trace(json!({"direction": "chromiumoxide", "step": "binding_verified", "product": live_product}));
    adapter.browser = Some(browser);

    // ---- Scenario steps.
    let empty = vec![];
    let steps: Vec<Value> = payload["steps"].as_array().unwrap_or(&empty).clone();
    for (idx, step) in steps.iter().enumerate() {
        let outcome = adapter.run_op(step).await;
        let (ok, value, err) = match outcome {
            Ok(v) => (true, Some(v), String::new()),
            Err(e) => (false, None, truncate(&e, 1000)),
        };
        adapter.trace(json!({
            "direction": "chromiumoxide", "step": idx, "op": step["op"], "selector": step["selector"],
            "ok": ok, "error": if err.is_empty() { Value::Null } else { json!(err) },
        }));
        if let Some(save_as) = step["save_as"].as_str() {
            let text = if ok {
                match &value {
                    Some(Value::Null) | None => "null".to_string(),
                    Some(v) => to_saved_string(v),
                }
            } else {
                format!("ERROR: {err}")
            };
            adapter.saved.insert(save_as.to_string(), text);
        }
        adapter.steps.push((ok, value, err));
    }

    let checks: Vec<Value> = payload["checks"].as_array().unwrap_or(&empty).clone();
    let mut rows = vec![json!({
        "name": "driver_connect", "status": "pass",
        "evidence": format!("chromiumoxide@{CLIENT_VERSION} bound to {live_product}"),
    })];
    let mut pass_count = 1;
    for (idx, check) in checks.iter().enumerate() {
        let (ok, evidence) = adapter.evaluate_check(check);
        if ok {
            pass_count += 1;
        }
        rows.push(json!({"name": check_name(check, idx), "status": if ok { "pass" } else { "fail" }, "evidence": evidence}));
    }
    let answer = adapter
        .saved
        .get("answer")
        .cloned()
        .unwrap_or_else(|| format!("{pass_count}/{} checks", rows.len()));
    let op_errors_total = adapter.steps.iter().filter(|(ok, _, _)| !ok).count();
    emit(json!({
        "ok": true,
        "answer": answer,
        "observations": {
            "checks": rows, "saved": adapter.saved, "binding": binding,
            "driver_ops": adapter.steps.len(), "driver_op_errors": op_errors_total,
            "failure_class": "cdp_semantic",
        },
        "metrics": {"cdp_call_count": adapter.op_calls, "cdp_error_count": adapter.op_errors, "ws_disconnect_count": 0},
    }));
    // The handler task keeps the runtime alive; the result is on stdout.
    std::process::exit(0);
}
