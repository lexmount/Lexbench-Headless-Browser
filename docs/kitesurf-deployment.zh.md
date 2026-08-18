# Kitesurf 评测环境部署（fixture 双源契约）

本分支（`kitesurf-eval`）评测远程端点形态的 Kitesurf。任务页面来自两类源，
职责与托管方式不同，这是本仓库的**正式决议**：

1. **静态源 = 本仓库 GitHub Pages。**`pages/` 目录随分支入库，经 Pages workflow
   发布到 `https://lexmount.github.io/Lexbench-Headless-Browser`，`v1/` 版本化路径。
   每个文件的 sha256 pin 在 `config/kitesurf_static_fixture.json`，运行前逐条校验。
2. **动态源 = 使用者自部署。**WebSocket echo、SSE、重定向链、auth/cart 会话
   mini-app、server-side grader、slow document、upload receipt 等 28 个动态探针
   需要真服务端行为，GitHub Pages 这类静态托管在结构上无法承载。官方**不托管**
   动态源：使用者在本机运行 harness 自带的 FixtureServer，并用 HTTPS 隧道暴露给
   远程端点。隧道 URL 是**运行参数**，不是结果的一部分。
3. **任何 Kitesurf run 之前强制 deployment-contract preflight。**
   `config/kitesurf_dynamic_fixture.json` 把 127 条静态路由的 sha256、28 个动态
   探针的行为断言、以及 FixtureServer 实现指纹（`contract_sha256`）一起 pin 死；
   校验不过就拒跑。URL 不是内容身份，契约才是。
4. **预期演化：** Kitesurf 发布本地 binary 后进入主分支 roster，本分支废弃。
5. **证据模型声明：**远程端点无 binary 指纹、无资源测量、共享基础设施、公网延迟
   计入任务预算，因此 `formal_score_eligible: false` 全程生效；其结果与四个本地
   引擎不同级，报告必须分栏并标注。

## 四条命令跑通

```bash
# 1. 起动态源（本机 FixtureServer，稳定端口）
python3 -m runner.run fixture-serve --port 8907

# 2. 开 HTTPS 隧道（标准示例：cloudflared quick tunnel；任何等价 HTTPS 隧道均可）
cloudflared tunnel --url http://127.0.0.1:8907
#    记下输出的 https://<random>.trycloudflare.com

# 3. 校验部署契约（静态 Pages 源 + 你的动态源，双源都要过）
python3 tools/kitesurf_static_fixture.py  --base-url https://lexmount.github.io/Lexbench-Headless-Browser \
    --output runs/kitesurf_static_verification.json
python3 tools/kitesurf_dynamic_fixture.py verify \
    --base-url https://<random>.trycloudflare.com \
    --output runs/kitesurf_dynamic_verification.json

# 4. 跑 recipe（校验产物在手之后才允许）
python3 tools/kitesurf_experiments.py run raw_full \
    --var fixture_base_url=https://<random>.trycloudflare.com \
    --var output=runs/kitesurf_raw_full
```

`tools/kitesurf_experiments.py list` 查看全部 recipe；`check` 验证 manifest；
`render` 只打印将执行的命令。

## 边界声明

- **延迟预算。**任务超时与本地引擎用同一套标准（多数 30s）。你的链路（本机 →
  隧道 → 远程端点 → 隧道 → 本机）全程计入。高延迟链路会把边缘任务推成
  timeout——这是部署属性，不是引擎属性；报告结论前先看
  `runs/<id>/results.jsonl` 里 timeout 的分布。
- **共享端点。**公共 Kitesurf 端点可能被他人同时使用，并发干扰无法归因。
  正式读数用 k=1 + 失败重跑裁定流（B 类裁定），不要并发多 recipe 打同一端点。
- **安全提示。**隧道把你本机的 FixtureServer 暴露到公网。FixtureServer 只服务
  fixture 树和确定性探针、不读取仓库外文件，但仍建议：用完即关隧道；不要复用
  长期域名；不要在 fixtures/ 里放任何私有内容。凭据类探针（auth flow）使用的
  是契约里 pin 死的测试凭据，不是秘密。

## 与主分支的关系

主分支只含四个本地固定二进制引擎，携带零 Kitesurf 代码。本分支在主分支之上
追加：runner 的通用 `remote_cdp` 身份契约（三字段同连接验证，见
`runner/scripts/adapters/PROTOCOL.md`）、recipe 机制、fixture 双源契约与本文档。
`config/kitesurf_experiments.json` 的 `base_main_at_consolidation` 记录了分叉的
主分支提交。
