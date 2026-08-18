<p align="center">
  <img src="docs/assets/lexbench-hero.png" alt="Lexbench-Headless-Browser — Benchmarking headless browsers as agent runtimes" width="100%">
</p>

<h1 align="center">Lexbench-Headless-Browser</h1>

<p align="center">
  <a href="https://x.com/LexmountAI"><img src="https://img.shields.io/badge/X-%40LexmountAI-000000?style=flat&logo=x&logoColor=white" alt="在 X 上关注 @LexmountAI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-2F80ED.svg" alt="许可证：AGPL-3.0"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh.md">中文</a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)"
            srcset="https://img.shields.io/badge/%E6%97%A0%E5%A4%B4%E6%B5%8F%E8%A7%88%E5%99%A8%E7%9A%84%E4%B8%8B%E4%B8%80%E4%B8%AA%E4%BD%BF%E7%94%A8%E8%80%85%E6%98%AF%20Agent%E3%80%82-3D74D9?style=for-the-badge">
    <img src="https://img.shields.io/badge/%E6%97%A0%E5%A4%B4%E6%B5%8F%E8%A7%88%E5%99%A8%E7%9A%84%E4%B8%8B%E4%B8%80%E4%B8%AA%E4%BD%BF%E7%94%A8%E8%80%85%E6%98%AF%20Agent%E3%80%82-2050B3?style=for-the-badge"
         alt="无头浏览器的下一个使用者是 Agent。" />
  </picture>
</p>

<p align="center"><b>1,928 道 task · <a href="#测试集构成">Playwright、Puppeteer、Selenium 等 13 个浏览器自动化工具</a></b></p>

Agent 正在接管原本需要人来做网页操作：搜索、比价、填表、下单。这些页面不再渲染给人看，而是让 Agent 进去读状态、执行操作、取回结果。浏览器由此从"给人用的工具"，变成了"Agent 的运行时"。

Agent 并不直接操作浏览器，中间隔着一整条控制链：工具层 → driver 客户端 → 控制协议 → 浏览器引擎。这条链目前没有事实标准，不同 Agent 框架各自选择了不同的路径：[browser-use](https://github.com/browser-use/browser-use) 用自己的 `cdp-use` 客户端，[hermes-agent](https://github.com/nousresearch/hermes-agent) 经由 [agent-browser](https://github.com/vercel-labs/agent-browser) 驱动会话，Google 官方的 [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) 则架在 Puppeteer 之上。一个引擎好不好用，取决于这条链上的每一环是否都走得通。

与此同时，[Moli](https://github.com/lexmount/moli)、[Lightpanda](https://github.com/lightpanda-io/browser)、[Obscura](https://github.com/h4ckf0r0day/obscura) 这批以"轻量"为卖点的新引擎，想在 Agent 场景里替代 Chrome。替代的前提有两个：其一，Chrome 生态里现成的 driver 连上来照样能工作；其二，协议连通之后，透过协议看到的页面语义也必须正确。

这个前提过去一直没有系统性的检验办法。[WPT](https://web-platform-tests.org/)（web-platform-tests）覆盖 Web 平台规范与跨浏览器互操作性，也包含 WebDriver 等标准化自动化协议的测试，但它的目标不是验证 Playwright、Puppeteer、Selenium、agent-browser 这些真实客户端栈能否端到端驱动某个候选引擎，也给不出这些路径在统一标准下的兼容性与资源对比。各引擎自己公布的接口支持和示例，同样无法给出跨引擎、可复现的横向比较。

Lexbench-Headless-Browser 把"这个前提"变成了可以测量、可以复现的测试。每道 task 从一条真实控制路径出发——或是裸 CDP，或是 13 个版本 driver 之一——完整走一遍控制链，最后验证操作是否真的完成、背后的页面语义是否正确。测出的结果同时告诉引擎开发者：缺哪些接口、具体缺在哪条 driver 路径上。Chrome 以"参照列"的身份跑同一套题：它不参与"替代"的比较，只用来确认题目本身在成熟引擎上确实能过，并作为资源消耗的基线——量化换掉 Chrome 之后，每道任务到底省下多少内存和 CPU。

## 结果

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/four-engine-overview-dark.png">
    <img alt="四个无头浏览器在 1,928 道任务上的成功率：Chrome 99.9%、Moli 80.7%、Lightpanda 43.8%、Obscura 39.5%" src="docs/assets/four-engine-overview-light.png" width="100%">
  </picture>
</div>

<div align="center">

| 引擎 | 版本 | 通过 | 任务成功率 |
|:---|:---|---:|---:|
| [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) | 151.0.7922.47 | 1,926 / 1,928 | **99.90%** |
| [Moli](https://github.com/lexmount/moli) | 0.1.1 | 1,556 / 1,928 | **80.71%** |
| [Lightpanda](https://github.com/lightpanda-io/browser) | 1.0.0-dev.321+b04c99a9 | 845 / 1,928 | **43.83%** |
| [Obscura](https://github.com/h4ckf0r0day/obscura) | 0.1.11 | 762 / 1,928 | **39.52%** |

</div>

<details>
<summary><b>五引擎结果速览（含 Kitesurf，1,308 道可比任务子集）</b></summary>

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/five-engine-caliber-b-dark.png">
    <img alt="五个无头浏览器在 1,308 道可比任务上的成功率：Chrome 99.8%、Moli 81.9%、Kitesurf 62.1%、Lightpanda 53.3%、Obscura 44.9%" src="docs/assets/five-engine-caliber-b-light.png" width="100%">
  </picture>
</div>

<div align="center">

| 引擎 | 可比子集（1,308 道） |
|:---|---:|
| [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) | 1,306 / 1,308 · **99.85%** |
| [Moli](https://github.com/lexmount/moli) | 1,071 / 1,308 · **81.88%** |
| [Kitesurf](https://blog.cloudflare.com/kitesurf/)（远程） | 812 / 1,308 · **62.08%** |
| [Lightpanda](https://github.com/lightpanda-io/browser) | 697 / 1,308 · **53.29%** |
| [Obscura](https://github.com/h4ckf0r0day/obscura) | 587 / 1,308 · **44.88%** |

</div>

这份对比取的是同一份 1,928 道任务集的一个子集：剔除掉面对远程端点失败、无法归因的题，对五个引擎一视同仁；剔除不代表这些题在 Kitesurf 提供本地二进制后就一定能通过。子集定义和完整报告见 [`kitesurf-eval` 分支](https://github.com/lexmount/Lexbench-Headless-Browser/blob/kitesurf-eval/README.zh.md)。

</details>

本次 run 的参数：run id `four_engine_full_20260812`、bench tag `2026.08.02-v0_4.1`、seed `official20260709`、k=3，共 23,136 条结果行。计分规则为 k=3 全通过制：每道题要尝试三次，三次全部成功才算通过；每个引擎只按自己的尝试计分（`--score-mode independent`），Chrome 仅作参照。完整报告见 [four-engine-report-20260812.md](docs/reports/four-engine-report-20260812.md)。

> [!NOTE]
> 如果你关心 Cloudflare 新发布的 agent-first 云端浏览器 [Kitesurf](https://blog.cloudflare.com/kitesurf/)，它的评测在 [`kitesurf-eval`](https://github.com/lexmount/Lexbench-Headless-Browser/blob/kitesurf-eval/README.zh.md) 分支。Kitesurf 目前只以远程端点形态存在，没有二进制、无法测量资源，五引擎结果在该分支发布。

## 资源开销

轻量引擎最大的卖点是资源消耗低——"替代 Chrome"的论据能不能成立，就得看到底省了多少。因此资源轮采用了和兼容性同等严格的方式。任务集取 `l1.raw_cdp`（375 道）加上 L2 的 `l2.web_platform` 子集（182 道），共 557 道；并且必须以 `--jobs 1` 串行执行，避免不同引擎之间互相争抢资源。所有资源统计只在这四个引擎共同通过的任务×尝试交集上计算（共 1,045 个样本）：只有大家都完成了同一批工作，比较各自的开销才有意义；某个引擎没通过的题，也不会计入它的资源账。

测量本身会带来误差——比如开启 profiler 会引入额外开销，所以采用两轮 A/B 设计：同一批任务先关掉 profiler 完整跑一轮作为基线，再开启 profiler 跑一轮，两轮之差就是观测行为自身造成的干扰。只有当干扰低于校准阈值（`resource_comparison_eligible: true`）时，数字才会被记录和公布。

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/efficiency-map-dark.png">
    <img alt="任务成功率与单题内存峰值中位数的关系：Chrome 99.9%、697 MiB，Moli 80.7%、92 MiB，Lightpanda 43.8%、34 MiB，Obscura 39.5%、39 MiB" src="docs/assets/efficiency-map-light.png" width="100%">
  </picture>
</div>

单题进程树内存峰值的中位数：Lightpanda 34 MiB、Obscura 39 MiB、Moli 92 MiB、Chrome 697 MiB；同一批任务的单题引擎 CPU 时间中位数分别是 36 ms、38 ms、101 ms、687 ms。细节与校准记录见[资源卡片](docs/reports/resource-card-20260812.md)。

## 测试集构成

当前 bench 版本 `2026.08.17-v0_4.2`：共 1,928 道 task、18 个子集，分两层（L1 / L2）。上文发布的 run 仍固定在 `2026.08.02-v0_4.1`——本次版本只是调整了 task 描述和不参与执行的元数据，没有改变任务集、driver、grader、fixture 或 capability 归属。

**L1：测协议与 driver 兼容性，共 1,740 道，走两条路径**

- 裸 CDP（`l1.raw_cdp`，375 道）：不经任何 driver 库，直接在 WebSocket 上按 CDP 协议通信，测的是最底层的协议连通性。
- 13 个固定版本的 driver（见下表）：其中 1,233 道由 116 个场景规格（scenario spec）跨这 13 个 driver 展开而成，即同一个行为在每个生态里各测一遍。不是每个行为都在每个 driver 上可表达；场景规格里未绑定某个 driver 的，必须写明跳过理由，`scenarios --check` 会逐条校验这些理由。

**L2：测 Web 平台语义，共 188 道（其中 182 道构成 `l2.web_platform` 子集）**

- 覆盖 DOM、存储、网络、Worker、CSSOM。
- 判定看的是页面最终行为，而不是协议回声：CDP 回了成功，但页面上什么都没发生，不算通过。
- 结果经能力映射表（capability map）归拢为 72 个能力项；带语义判定的能力项按尝试计分（k=3），报告里 L2 一列共 192 个计分单元。

13 个 driver 横跨五个语言生态，版本全部固定在 [`harness_pins.json`](harness_pins.json)，`doctor` 会在每次 run 前逐一校验：

<div align="center">

| Driver | 生态 | 控制路径 | 版本 pin |
|:---|:---|:---|:---|
| [playwright-core](https://github.com/microsoft/playwright) | Node | CDP（框架 API） | 1.61.1 |
| [puppeteer-core](https://github.com/puppeteer/puppeteer) | Node | CDP（框架 API） | 25.3.0 |
| [stagehand](https://github.com/browserbase/stagehand) | Node | CDP（框架 API） | 3.7.0 |
| [selenium](https://github.com/SeleniumHQ/selenium) | Python | WebDriver | 4.46.0 |
| [chrome-remote-interface](https://github.com/cyrus-and/chrome-remote-interface) | Node | CDP（薄客户端） | 0.34.0 |
| [cdp-use](https://github.com/browser-use/cdp-use) | Python | CDP（薄客户端） | 1.4.5 |
| [pydoll](https://github.com/autoscrape-labs/pydoll) | Python | CDP（生态 driver） | 2.23.1 |
| [chromedp](https://github.com/chromedp/chromedp) | Go | CDP（生态 driver） | v0.16.0 |
| [rod](https://github.com/go-rod/rod) | Go | CDP（生态 driver） | v0.116.2 |
| [chromiumoxide](https://github.com/mattsse/chromiumoxide) | Rust | CDP（生态 driver） | 0.9.1 |
| [ferrum](https://github.com/rubycdp/ferrum) | Ruby | CDP（生态 driver） | 0.17.2 |
| [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | Node | MCP → Puppeteer → CDP | 1.6.0 |
| [agent-browser](https://github.com/vercel-labs/agent-browser) | Node | CLI 会话 → CDP | 0.31.1 |

</div>

任务集由 `manifest.json` 固定：同一个 bench 版本内任务内容冻结，任何改动都必须提升版本号。任务集也会随引擎与 driver 生态的演进持续扩充，新增的子集和 task 将以新的 bench 版本发布。

## 评测方法

每次尝试都要过两道身份校验：连接前先做 HTTP `/json/version` 探测，连接成功后，在传输层上再验证一次。校验不过的记录记为 `infra`（基础设施问题），不计任何分数。这道防线挡住的典型情况是：候选引擎在底下悄悄换成了 Chrome。

每条结果行都带一个状态（`pass`、`fail`、`unsupported`、`timeout`、`crash`、`infra`）；失败的行还会记录 `failure.class` 与 `failure.origin`，便于定位"是哪种失败、失败在哪一环"。

`docs/reports/` 下的报告由读取 run 的 `results.jsonl` 的生成器产出。同一份 run 再跑一次生成器，输出逐字节相同——保证报告可复现。

功能轮与资源轮从不共用同一次 run。资源数字只来自上面那套 A/B 协议，只有 `resource_comparison_eligible` 为真时才会公布。

`run_manifest.json` 记录了 runner 源码树、fixture 树和编译后 adapter 二进制的摘要，任何人都能核对某次 run 到底由哪份代码产出。时间戳统一使用 UTC；结果行不包含绝对路径；使用 `--provenance-level minimal` 时只保留硬件事实、去掉部署指纹。

## 文档

<div align="center">

| 文档 | 回答什么 |
|:---|:---|
| [docs/RUNNING.zh.md](docs/RUNNING.zh.md) | 怎么装引擎和 driver，怎么把 bench 跑起来 |
| [docs/REPRODUCE.zh.md](docs/REPRODUCE.zh.md) | 怎么复现已发布的 run，怎么重新生成报告 |
| [docs/RESULTS.zh.md](docs/RESULTS.zh.md) | 结果怎么读：判定边界与适用范围 |
| [docs/reports/](docs/reports/) | 报告本身，由 run 产物生成 |

</div>

## 仓库结构

<div align="center">

| 路径 | 内容 |
|:---|:---|
| [`runner/`](runner/) | 评测框架：`run.py`（编排）、`resources.py`、`bindings.py`、`scenario.py`，以及 `scripts/adapters/` 下的 13 个 driver adapter |
| [`tasks/`](tasks/) | 1,928 道任务定义（`L1/`、`L2/`） |
| [`fixtures/`](fixtures/) | 由框架自托管的确定性 fixture 树 |
| [`config/`](config/) | driver 绑定、L2 语义 capability map、CDP 覆盖豁免表 |
| [`generated/`](generated/) | 渲染产物：CDP 覆盖矩阵、driver 绑定矩阵 |
| [`manifest.json`](manifest.json) | bench id 与版本、subset 注册表 |
| [`harness_pins.json`](harness_pins.json) | 各生态的 driver 版本 pin |
| [`test/`](test/) | 框架单元测试（stdlib + pytest，不需要引擎二进制） |
| [`docs/`](docs/) | 怎么跑、怎么复现、结果怎么读，以及报告 |

</div>

## 许可证

AGPL-3.0，见 [LICENSE](LICENSE)。上游 task 与 fixture 的署名和许可信息集中保留在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
