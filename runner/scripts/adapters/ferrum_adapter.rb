#!/usr/bin/env ruby
# frozen_string_literal: true

# Ferrum scenario adapter.
#
# Drives the engine under test with the pinned `ferrum` gem —
# the Ruby ecosystem's headless-Chrome driver (the engine under Cuprite).
# Speaks the abb_scenario_adapter/1 contract (PROTOCOL.md in this directory):
# payload JSON on stdin, framework_probe.js result contract on stdout,
# mandatory two-layer binding gate, shared op vocabulary.
#
# The adapter exercises Ferrum's own surface — Browser.new(ws_url:),
# create_page, Page#go_to, Node#click/#type, Page#evaluate — because the
# column measures whether Ferrum's abstractions hold on each engine.
# ws_url connects to the runner-verified browser websocket exactly as given
# and never launches a browser; the adapter never calls quit.

require "json"
require "net/http"
require "openssl"
require "timeout"
require "ferrum"

UNSUPPORTED_MARKERS = ["not found", "wasn't found", "unsupported", "unknown method", "not implemented", "not supported"].freeze

def emit(obj)
  $stdout.write(JSON.generate(obj))
end

def emit_infra(message, binding_info, calls, errs)
  emit(
    "ok" => false,
    "error" => { "class" => "script_error", "message" => message },
    "observations" => { "binding" => binding_info },
    "metrics" => { "cdp_call_count" => calls, "cdp_error_count" => errs, "ws_disconnect_count" => 0 }
  )
end

def apply_cleanup_contract(outcome, cleanup, label)
  observations = (outcome["observations"] || {}).merge(
    "target_cleanup" => cleanup,
    "isolation_restored" => cleanup["confirmed"] == true
  )
  return outcome.merge("observations" => observations) if cleanup["confirmed"] == true

  {
    "ok" => false,
    "status" => "infra",
    "error" => {
      "class" => "script_error",
      "message" => "#{label} target cleanup was not confirmed: #{cleanup.inspect}"
    },
    "answer" => outcome["answer"],
    "observations" => observations.merge("primary_outcome" => outcome),
    "metrics" => outcome["metrics"] || {}
  }
end

def to_saved_string(value)
  case value
  when nil then "null"
  when true then "true"
  when false then "false"
  when String then value
  when Numeric
    value == value.to_i ? value.to_i.to_s : value.to_s
  else
    JSON.generate(value)
  end
end

def http_json(url, timeout_s)
  uri = URI(url)
  http = Net::HTTP.new(uri.host, uri.port)
  http.open_timeout = timeout_s
  http.read_timeout = timeout_s
  JSON.parse(http.get(uri.request_uri).body)
end

class Adapter
  attr_reader :saved, :steps, :op_calls, :op_errors
  attr_accessor :browser, :page

  def initialize(payload)
    @payload = payload
    @task_url = payload.fetch("task_url")
    uri = URI(@task_url)
    @fixture_origin = "#{uri.scheme}://#{uri.host}:#{uri.port}"
    @fixture_host = "#{uri.host}:#{uri.port}"
    @artifact_dir = payload["artifact_dir"] || "."
    @action_timeout_ms = (payload["action_timeout_ms"] || 8000).to_i
    task_timeout_ms = (payload["task_timeout_ms"] || 30_000).to_i
    # Leave a 3s reserve for check evaluation and result emission.
    @budget_deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + (task_timeout_ms - 3000) / 1000.0
    @saved = {}
    @steps = []
    @op_calls = 0
    @op_errors = 0
    @pages = []
    @page_creations = []
    @trace_path = File.join(@artifact_dir, "cdp.jsonl")
    File.write(@trace_path, "")
  rescue Errno::ENOENT
    nil
  end

  def create_tracked_page(timeout_ms)
    creation = {
      "attempt" => @page_creations.length + 1,
      "state" => "requested"
    }
    @page_creations << creation
    begin
      created = call_op(timeout_ms) { @browser.create_page }
      target_id = created&.target_id.to_s
      if target_id.empty?
        creation["state"] = "ambiguous"
        raise "Ferrum created a page without exposing its target id"
      end
      creation.merge!("state" => "created", "target_id" => target_id, "page" => created)
      @pages << created
      created
    rescue StandardError => e
      creation["state"] = "ambiguous" unless creation["state"] == "created"
      creation["error"] = e.message.to_s[0, 500]
      raise
    end
  end

  def cleanup_pages
    attempts = []
    @pages.reverse_each do |created|
      creation = @page_creations.find { |candidate| candidate["page"].equal?(created) }
      target_id = creation && creation["target_id"]
      closed = false
      2.times do |offset|
        attempt = offset + 1
        begin
          raise "created page has no target id" if target_id.to_s.empty?

          response = Timeout.timeout(3) do
            @browser.command("Target.closeTarget", targetId: target_id)
          end
          success = response.is_a?(Hash) && response["success"] == true
          closed = success
          attempts << {
            "target_id" => target_id,
            "attempt" => attempt,
            "success" => response.is_a?(Hash) ? response["success"] : nil,
            "confirmed" => closed
          }
        rescue Timeout::Error
          attempts << {
            "target_id" => target_id,
            "attempt" => attempt,
            "confirmed" => false,
            "timed_out" => true,
            "error" => "Target.closeTarget timeout"
          }
          break
        rescue StandardError => e
          attempts << {
            "target_id" => target_id,
            "attempt" => attempt,
            "confirmed" => false,
            "error" => e.message.to_s[0, 500]
          }
        end
        break if closed
      end
      creation["state"] = closed ? "closed" : "cleanup_unconfirmed" if creation
      created.close_connection if closed
    rescue StandardError
      # Cleanup evidence already records the protocol outcome. Closing the
      # local per-page subscriber is best effort and cannot confirm a target.
      nil
    end

    confirmed = @page_creations.all? { |creation| creation["state"] == "closed" }
    {
      "backend" => "ferrum.Target.closeTarget",
      "required" => !@page_creations.empty?,
      "confirmed" => confirmed,
      "same_connection_as_task" => true,
      "creation_attempts" => @page_creations.map { |creation| creation.reject { |key, _| key == "page" } },
      "attempts" => attempts
    }
  end

  def trace(obj)
    File.open(@trace_path, "a") { |f| f.puts(JSON.generate(obj.merge("ts" => Time.now.utc.iso8601))) }
  rescue StandardError
    nil # trace failures must not fail the probe
  end

  def substitute(raw)
    raw.to_s
       .gsub("{fixture_url}", @task_url)
       .gsub("{fixture_origin}", @fixture_origin)
       .gsub("{fixture_host}", @fixture_host)
       .gsub("{artifact_dir}", @artifact_dir)
  end

  # Clamp an op's wait to the remaining task budget so the adapter always
  # emits a graded result instead of being killed mid-run.
  def op_timeout_s(timeout_ms)
    remaining = @budget_deadline - Process.clock_gettime(Process::CLOCK_MONOTONIC)
    raise "task budget exhausted before op could run" if remaining <= 0

    [timeout_ms / 1000.0, remaining].min
  end

  def call_op(timeout_ms)
    @op_calls += 1
    seconds = op_timeout_s(timeout_ms)
    Timeout.timeout(seconds) { yield }
  rescue Timeout::Error
    @op_errors += 1
    raise "timeout after #{timeout_ms}ms"
  rescue StandardError => e
    @op_errors += 1
    raise e.message.to_s[0, 500]
  end

  def eval_value(timeout_ms, expression)
    # eval() of the raw program keeps completion-value semantics so
    # multi-statement expressions ("a; b; c") stay legal.
    quoted = JSON.generate(expression)
    call_op(timeout_ms) { @page.evaluate("(() => eval(#{quoted}))()") }
  end

  def sel_expr(sel, body)
    quoted = JSON.generate(sel)
    escaped = sel.gsub('"', '\\"')
    "(() => { const el = document.querySelector(#{quoted}); if (!el) throw new Error(\"no element matches #{escaped}\"); #{body} })()"
  end

  def poll_until(timeout_ms, expression, what)
    deadline = Process.clock_gettime(Process::CLOCK_MONOTONIC) + op_timeout_s(timeout_ms)
    loop do
      value = begin
        eval_value(2000, expression)
      rescue StandardError
        nil # evaluation context may be mid-navigation; retry
      end
      return if value && value != false && value != ""
      raise "timeout after #{timeout_ms}ms waiting for #{what}" if Process.clock_gettime(Process::CLOCK_MONOTONIC) > deadline

      sleep 0.05
    end
  end

  def settle_navigation(timeout_ms, target)
    uri = URI(target)
    want = JSON.generate(uri.path + (uri.query ? "?#{uri.query}" : ""))
    poll_until(timeout_ms, "document.readyState === \"complete\" && (location.pathname + location.search) === #{want}", "navigation to #{target}")
  end

  def find_node(timeout_ms, sel)
    call_op(timeout_ms) do
      node = @page.at_css(sel)
      raise "no element matches #{sel}" unless node

      node
    end
  end

  def ax_value(value)
    value = value["value"] if value.is_a?(Hash) && value.key?("value")
    value.nil? ? "" : value.to_s
  end

  def full_ax_tree(timeout_ms)
    call_op(timeout_ms) { @page.command("Accessibility.enable") }
    result = call_op(timeout_ms) { @page.command("Accessibility.getFullAXTree") }
    result["nodes"].is_a?(Array) ? result["nodes"] : []
  end

  def ax_identity(nodes, role, name)
    node = nodes.find do |candidate|
      ax_value(candidate["role"]) == role && ax_value(candidate["name"]) == name
    end
    raise "AX node role=#{role.inspect} name=#{name.inspect} not found" unless node

    backend_id = node["backendDOMNodeId"]
    raise "AX node role=#{role.inspect} name=#{name.inspect} has no backendDOMNodeId" if backend_id.nil?

    "role=#{role}|name=#{name}|backendDOMNodeId=#{backend_id}"
  end

  def format_computed_style(step, computed_style)
    required = step["required_properties"] || %w[display visibility opacity pointer-events]
    minimum = (step["min_property_count"] || 100).to_i
    properties = {}
    (computed_style.is_a?(Array) ? computed_style : []).each do |entry|
      next unless entry.is_a?(Hash) && entry.key?("name")

      properties[entry["name"].to_s] = entry["value"].to_s
    end
    readable = required.all? { |name| properties.key?(name) && !properties[name].empty? }
    prefix = properties.length >= minimum && readable ? "breadth-ok" : "breadth-insufficient"
    details = required.map { |name| "#{name}=#{properties.fetch(name, '<missing>')}" }
    "#{prefix}|count=#{properties.length}|#{details.join('|')}"
  end

  def run_op(step)
    op = step["op"]
    sel = step["selector"] ? substitute(step["selector"]) : nil
    timeout = step["timeout_ms"] ? step["timeout_ms"].to_i : @action_timeout_ms

    case op
    when "wait_ms"
      sleep((step["ms"] || 100).to_f / 1000.0)
      nil
    when "version"
      call_op(timeout) { @browser.command("Browser.getVersion")["product"] }
    when "user_agent"
      call_op(timeout) { @browser.command("Browser.getVersion")["userAgent"] }
    when "new_page"
      @page = create_tracked_page(timeout)
      "page_created"
    when "goto"
      target = step["url"] ? substitute(step["url"]) : @task_url
      call_op(timeout) { @page.go_to(target) }
      settle_navigation(timeout, target)
      "navigated"
    when "reload"
      eval_value(timeout, 'window.__abb_reload_probe = 1, "marked"')
      call_op(timeout) { @page.refresh }
      poll_until(timeout, 'document.readyState === "complete" && !window.__abb_reload_probe', "reload to settle")
      "reloaded"
    when "go_back", "go_forward"
      nav_nonce = "np#{Process.clock_gettime(Process::CLOCK_REALTIME, :nanosecond)}"
      eval_value(timeout, "window.__abb_nav_probe = '#{nav_nonce}|' + location.href, 'marked'")
      call_op(timeout) { op == "go_back" ? @page.back : @page.forward }
      poll_until(timeout, "document.readyState === 'complete' && window.__abb_nav_probe !== '#{nav_nonce}|' + location.href", op)
      "ok"
    when "click"
      times = (step["times"] || 1).to_i
      times.times do
        node = find_node(timeout, sel)
        call_op(timeout) { node.click }
      end
      "clicked x#{times}"
    when "fill"
      value = substitute(step["value"] || "")
      node = find_node(timeout, sel)
      eval_value(timeout, sel_expr(sel, 'el.focus(); if (typeof el.select === "function") el.select(); return "focused";'))
      call_op(timeout) { node.type(value) }
      "filled"
    when "type"
      text = substitute(step["text"] || "")
      node = find_node(timeout, sel)
      call_op(timeout) { node.focus.type(text) }
      "typed"
    when "press"
      key = step["key"].to_s
      raise "unsupported key #{key.inspect} for press" unless %w[Enter Tab Escape Backspace].include?(key)

      node = find_node(timeout, sel)
      call_op(timeout) { node.focus.type(key.to_sym) }
      "pressed #{key}"
    when "check"
      already = eval_value(timeout, sel_expr(sel, "return !!el.checked;"))
      unless already == true
        node = find_node(timeout, sel)
        call_op(timeout) { node.click }
      end
      "checked"
    when "select_option"
      value = substitute(step["value"] || "")
      quoted = JSON.generate(value)
      eval_value(timeout, sel_expr(sel, "el.value = #{quoted}; el.dispatchEvent(new Event(\"input\", {bubbles: true})); el.dispatchEvent(new Event(\"change\", {bubbles: true})); return [el.value];"))
    when "focus"
      eval_value(timeout, sel_expr(sel, 'el.focus(); return "focused";'))
      "focused"
    when "evaluate"
      eval_value(timeout, substitute(step["expression"]))
    when "wait_for_function"
      poll_until(timeout, substitute(step["expression"]), "predicate")
      "predicate_true"
    when "wait_for_selector"
      quoted = JSON.generate(sel)
      expression =
        case step["state"]
        when "hidden", "detached"
          "(() => { const el = document.querySelector(#{quoted}); if (!el) return true; const s = window.getComputedStyle(el); return s.display === \"none\" || s.visibility === \"hidden\"; })()"
        when "visible"
          "(() => { const el = document.querySelector(#{quoted}); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== \"none\" && s.visibility !== \"hidden\"; })()"
        else
          "!!document.querySelector(#{quoted})"
        end
      poll_until(timeout, expression, "selector #{sel}")
      "selector_ready"
    when "text_content"
      eval_value(timeout, sel_expr(sel, "return el.textContent;"))
    when "inner_text"
      eval_value(timeout, sel_expr(sel, "return el.innerText;"))
    when "get_attribute"
      name = JSON.generate(step["name"].to_s)
      eval_value(timeout, sel_expr(sel, "return el.getAttribute(#{name});"))
    when "input_value"
      eval_value(timeout, sel_expr(sel, "return el.value;"))
    when "count"
      eval_value(timeout, "document.querySelectorAll(#{JSON.generate(sel)}).length")
    when "is_visible"
      quoted = JSON.generate(sel)
      eval_value(timeout, "(() => { const el = document.querySelector(#{quoted}); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== \"none\" && s.visibility !== \"hidden\"; })()")
    when "is_checked"
      eval_value(timeout, sel_expr(sel, "return !!el.checked;"))
    when "is_enabled"
      eval_value(timeout, sel_expr(sel, "return !el.disabled;"))
    when "ax_snapshot"
      full_ax_tree(timeout)
    when "ax_node_identity"
      role = step["role"].to_s
      name = substitute(step["name"].to_s)
      identity = ax_identity(full_ax_tree(timeout), role, name)
      if step["compare_to"]
        before = @saved[step["compare_to"].to_s]
        before == identity ? "stable|#{identity}" : "changed|before=#{before}|after=#{identity}"
      else
        identity
      end
    when "computed_style_breadth"
      doc = call_op(timeout) { @page.command("DOM.getDocument", depth: 0) }
      root_id = doc.dig("root", "nodeId")
      raise "DOM.getDocument returned no root nodeId" unless root_id

      found = call_op(timeout) do
        @page.command("DOM.querySelector", nodeId: root_id, selector: sel)
      end
      node_id = found["nodeId"]
      raise "no element matches #{sel}" unless node_id && !node_id.zero?

      call_op(timeout) { @page.command("CSS.enable") }
      result = call_op(timeout) do
        @page.command("CSS.getComputedStyleForNode", nodeId: node_id)
      end
      format_computed_style(step, result["computedStyle"])
    when "title"
      eval_value(timeout, "document.title")
    when "url"
      eval_value(timeout, "location.href")
    else
      raise "unknown op #{op.inspect}"
    end
  end

  def evaluate_check(check)
    kind = check["kind"]
    name = check["name"]
    case kind
    when "saved_equals"
      value = @saved[name]
      expected = check["expected"].to_s
      [!value.nil? && value == expected, "#{name}=#{value.nil? ? 'null' : value.inspect} expected=#{expected.inspect}"]
    when "saved_contains", "saved_not_contains"
      value = @saved[name]
      want = substitute(check["expected"].to_s)
      contains = !value.nil? && value.include?(want)
      ok = kind == "saved_contains" ? contains : (!value.nil? && !contains)
      clause = kind == "saved_contains" ? "must contain" : "must NOT contain"
      [ok, "#{name}=#{value.nil? ? 'null' : value[0, 300].inspect} #{clause} #{want.inspect}"]
    when "saved_truthy"
      value = @saved[name]
      truthy = !value.nil? && !["", "undefined", "null", "false"].include?(value) && !value.start_with?("ERROR:")
      [truthy, "#{name}=#{value.nil? ? 'null' : value[0, 300].inspect}"]
    when "step_ok", "step_fails"
      idx = check["step"].to_i
      row = @steps[idx] || {}
      ok_flag = row[:ok] ? true : false
      err_text = row[:err].to_s.empty? ? "none" : row[:err]
      if kind == "step_ok"
        [ok_flag, "step #{idx} ok=#{ok_flag} error=#{err_text}"]
      else
        [!row.empty? && !ok_flag, "step #{idx} ok=#{ok_flag} (must fail) error=#{err_text}"]
      end
    when "file_nonempty"
      path = substitute(check["path"].to_s)
      size = File.exist?(path) ? File.size(path) : 0
      [size.positive?, "#{path} size=#{size}"]
    when "any_of"
      results = (check["checks"] || []).map { |sub| evaluate_check(sub) }
      evidence = results.map { |ok, ev| "#{ok ? 'pass' : 'fail'}: #{ev}" }.join(" | ")
      [results.any? { |ok, _| ok }, evidence]
    else
      [false, "unknown check kind #{kind}"]
    end
  end
end

def check_name(check, idx)
  label = check["label"].to_s
  return label unless label.empty?

  kind = check["kind"].to_s
  kind.empty? ? "check#{idx}" : kind
end

payload = begin
  JSON.parse($stdin.read)
rescue StandardError => e
  emit("ok" => false, "error" => { "class" => "script_error", "message" => "invalid payload JSON on stdin: #{e}" }, "observations" => {}, "metrics" => {})
  exit 0
end

browser_ws = payload["browser_ws"].to_s
cdp_port = payload["cdp_port"].to_i
expect_product = payload["expect_product"].to_s
expect_ua = payload["expect_ua"].to_s
expect_live = payload["expect_product_live"].to_s
expect_live = expect_product if expect_live.empty?
identity_fields = %w[product protocolVersion revision].freeze
expected_remote_identity = if payload["remote_cdp"] == true
                             source = payload["expected_remote_identity"] || {}
                             identity_fields.to_h { |field| [field, source[field].to_s] }
                           end
checks = payload["checks"] || []
client_version = Gem::Specification.find_by_name("ferrum").version.to_s

binding_info = {
  "driver" => "ferrum", "browser_ws" => browser_ws,
  "expect_product" => expect_product, "verified" => false, "gate" => nil,
  "client_version" => client_version
}

# Ferrum 0.17.2's WebSocket transport creates an SSLSocket without assigning
# the URI hostname, so SNI-based remote WSS endpoints can reject the TLS
# handshake before any CDP data is sent. Preserve the unmodified local route;
# for an explicitly marked remote-CDP experiment, supply only the missing SNI
# hostname while continuing to use Ferrum's own Browser/Page surface.
if payload["remote_cdp"] == true && URI(browser_ws).scheme == "wss"
  remote_sni_host = URI(browser_ws).host
  sni_patch = Module.new do
    define_method(:connect) do |*args|
      self.hostname = remote_sni_host if respond_to?(:hostname=)
      super(*args)
    end
  end
  OpenSSL::SSL::SSLSocket.prepend(sni_patch)
  binding_info["transport_patch"] = "remote_wss_sni_hostname"
end
if browser_ws.empty? || payload["task_url"].to_s.empty?
  emit_infra("payload requires browser_ws and task_url", binding_info, 0, 0)
  exit 0
end
if expected_remote_identity && identity_fields.any? { |field| expected_remote_identity[field].empty? }
  emit_infra("binding gate: expected_remote_identity requires product/protocolVersion/revision", binding_info, 0, 0)
  exit 0
end

require "fileutils"
FileUtils.mkdir_p(payload["artifact_dir"] || ".")
adapter = Adapter.new(payload)

# ---- Binding gate 1/2: HTTP identity of the endpoint, verbatim.
if cdp_port.zero? || expect_product.empty?
  emit_infra("binding gate: cdp_port and expect_product are required (refusing to run unverified)", binding_info, 0, 0)
  exit 0
end
version_info = begin
  http_json("http://127.0.0.1:#{cdp_port}/json/version", 4)
rescue StandardError => e
  emit_infra("binding gate: /json/version unreachable on port #{cdp_port}: #{e}", binding_info, 0, 0)
  exit 0
end
http_product = (version_info["Browser"] || version_info["Product"] || "").to_s
http_ua = (version_info["User-Agent"] || "").to_s
binding_info["http_product"] = http_product
if http_product != expect_product || (!expect_ua.empty? && http_ua != expect_ua)
  emit_infra("binding gate: endpoint on port #{cdp_port} reports product=#{http_product.inspect} ua=#{http_ua.inspect}; expected product=#{expect_product.inspect} — refusing to run against an unverified engine", binding_info, 0, 0)
  exit 0
end
ws_from_version = version_info["webSocketDebuggerUrl"].to_s
unless ws_from_version.empty? || ws_from_version == browser_ws
  emit_infra("binding gate: browser_ws #{browser_ws} != verified endpoint #{ws_from_version}", binding_info, 0, 0)
  exit 0
end
binding_info["gate"] = "http_json_version"

# ---- Connect + binding gate 2/2: live-transport identity.
connect_error = nil
live_identity = nil
live_product = nil
browser = nil
begin
  Timeout.timeout((payload["connect_timeout_ms"] || 15_000).to_i / 1000.0) do
    browser = Ferrum::Browser.new(ws_url: browser_ws)
    live_identity = browser.command("Browser.getVersion")
    live_product = live_identity["product"].to_s
  end
rescue StandardError, Timeout::Error => e
  connect_error = e.message.to_s[0, 1000]
end
adapter.trace("direction" => "ferrum", "step" => "connect", "ok" => connect_error.nil?, "error" => connect_error)

if connect_error
  # A refused/failed connect is a genuine compatibility result: the engine
  # cannot be driven by this client. Grade every check as failed.
  rows = [{ "name" => "driver_connect", "status" => "fail",
            "evidence" => "ferrum@#{client_version} could not drive #{browser_ws}: #{connect_error}" }]
  checks.each_with_index do |check, idx|
    rows << { "name" => check_name(check, idx), "status" => "fail", "evidence" => "client did not connect; scenario not executed" }
  end
  outcome = {
    "ok" => true,
    "answer" => "0/#{rows.length} checks",
    "observations" => { "checks" => rows, "saved" => {}, "binding" => binding_info,
                        "connect_error" => connect_error, "failure_class" => "cdp_semantic" },
    "metrics" => { "cdp_call_count" => 1, "cdp_error_count" => 1, "ws_disconnect_count" => 0 }
  }
  adapter.browser = browser
  emit(apply_cleanup_contract(outcome, adapter.cleanup_pages, "Ferrum"))
  exit 0
end

binding_info["expect_product_live"] = expect_live
binding_info["live_product"] = live_product
binding_info["live_check"] = "ferrum_browser_get_version"
identity_verified = live_product == expect_live
if expected_remote_identity
  actual_remote_identity = identity_fields.to_h { |field| [field, (live_identity || {})[field].to_s] }
  mismatches = identity_fields.select { |field| actual_remote_identity[field] != expected_remote_identity[field] }
  binding_info.merge!(
    "transport" => "remote_cdp",
    "expected" => expected_remote_identity,
    "actual" => actual_remote_identity,
    "compared_fields" => identity_fields,
    "mismatches" => mismatches,
    "verified" => mismatches.empty?,
    "same_connection_as_task" => true,
    "reconnect_allowed" => false
  )
  identity_verified = mismatches.empty?
end
unless identity_verified
  outcome = {
    "ok" => false,
    "error" => {
      "class" => "script_error",
      "message" => "binding gate: live ferrum transport identity does not match the expected engine: #{binding_info['actual'].inspect}"
    },
    "observations" => { "binding" => binding_info },
    "metrics" => { "cdp_call_count" => 1, "cdp_error_count" => 1, "ws_disconnect_count" => 0 }
  }
  adapter.browser = browser
  emit(apply_cleanup_contract(outcome, adapter.cleanup_pages, "Ferrum"))
  exit 0
end
binding_info["verified"] = true
adapter.trace("direction" => "ferrum", "step" => "binding_verified", "identity" => binding_info["actual"] || { "product" => live_product })
adapter.browser = browser

# ---- Scenario steps.
(payload["steps"] || []).each_with_index do |step, idx|
  value = nil
  err = nil
  begin
    value = adapter.run_op(step)
  rescue StandardError => e
    err = e.message.to_s[0, 1000]
  end
  ok = err.nil?
  adapter.steps << { ok: ok, value: value, err: err }
  adapter.trace("direction" => "ferrum", "step" => idx, "op" => step["op"], "selector" => step["selector"], "ok" => ok, "error" => err)
  if step["save_as"]
    adapter.saved[step["save_as"]] = ok ? (value.nil? ? "undefined" : to_saved_string(value)) : "ERROR: #{err}"
  end
end

rows = [{ "name" => "driver_connect", "status" => "pass", "evidence" => "ferrum@#{client_version} bound to #{live_product}" }]
pass_count = 1
checks.each_with_index do |check, idx|
  ok, evidence = adapter.evaluate_check(check)
  pass_count += 1 if ok
  rows << { "name" => check_name(check, idx), "status" => ok ? "pass" : "fail", "evidence" => evidence }
end

answer = adapter.saved.fetch("answer", "#{pass_count}/#{rows.length} checks")
op_error_rows = adapter.steps.count { |row| !row[:ok] }
outcome = {
  "ok" => true,
  "answer" => answer,
  "observations" => {
    "checks" => rows, "saved" => adapter.saved, "binding" => binding_info,
    "driver_ops" => adapter.steps.length, "driver_op_errors" => op_error_rows,
    "failure_class" => "cdp_semantic"
  },
  "metrics" => { "cdp_call_count" => adapter.op_calls, "cdp_error_count" => adapter.op_errors, "ws_disconnect_count" => 0 }
}
emit(apply_cleanup_contract(outcome, adapter.cleanup_pages, "Ferrum"))
# Never browser.quit: with ws_url that could reach the engine under test.
exit 0
