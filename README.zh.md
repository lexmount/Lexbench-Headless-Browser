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

<p align="center"><strong>无头浏览器的下一个使用者是 Agent。</strong></p>

随着 Agent 越来越多地接管搜索、比价、填表、下单这些原本由人完成的网页操作，页面不再渲染给人看，而是作为 Agent 的执行环境：Agent 在里面读页面状态、执行操作、取回结果，浏览器从展示工具变成了运行时。

Agent 不直接碰浏览器，中间隔着一整条控制链：工具层、driver、控制协议，最后才到引擎。而且这条链没有事实标准，不同 Agent 框架选择了不同的控制路径——[browser-use](https://github.com/browser-use/browser-use) 用自研的 `cdp-use` 客户端，[hermes-agent](https://github.com/nousresearch/hermes-agent) 通过 [agent-browser](https://github.com/vercel-labs/agent-browser)等多种方式驱动会话，Google 官方的 [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) 则架在 Puppeteer 上。三个 Agent，三条不同的 driver 路径。一个引擎"好不好用"，由这些路径上的每一环是否走得通决定，而不是由引擎自己宣称什么决定。

恰好在这个时间点，出现了一批新引擎，比如 [Moli](https://github.com/lexmount/moli)、[Lightpanda](https://github.com/lightpanda-io/browser)、[Obscura](https://github.com/h4ckf0r0day/obscura)，它们以轻量为卖点，想在 Agent 场景里替代 Chrome。替代要成立，前提是 Chrome 生态里的这些 driver 连上来照样能工作，而且在协议连通之后，透过它看到的页面语义也得是对的。

但这个前提一直没有系统性的测法。[WPT](https://web-platform-tests.org/)（web-platform-tests）是面向 Web 平台规范与跨浏览器互操作性的测试套件，也包含 WebDriver 等标准化自动化协议的测试，但它的目标不是验证 Playwright、Puppeteer、Selenium、agent-browser 这些真实客户端栈能否端到端驱动一个候选引擎，也不提供这些路径在统一口径下的兼容性与资源评测。各引擎项目自己公布的接口支持和示例，同样给不出跨引擎、统一口径、可复现的比较。

Lexbench-Headless-Browser 就是为了给社区提供这个工具，并帮助 browser developer 持续迭代浏览器的功能覆盖面：每道 task 都从一个真实 driver 出发走完整条控制链——裸 CDP、WebDriver，或十三个固定版本的 driver 之一——然后检查操作有没有完成、它背后的页面语义对不对。Chrome 以参照列的身份跑同一套题，以回答替代后的另一半问题：如果换掉 Chrome 之后，每道任务省下了多少内存和 CPU。

## Results

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

实验 settings：Run_id `four_engine_full_20260812`，bench_tag `2026.08.02-v0_4.1`，seed `official20260709`，k=3，共 23,136 条结果行。一道题三次尝试全部通过才算通过。每个引擎只按自己的尝试计分（`--score-mode independent`），Chrome 在表里是参照列。完整报告见 [docs](docs/reports/four-engine-report-20260812.md)。

> [!NOTE]
> 如果你关心 [Kitesurf](https://blog.cloudflare.com/kitesurf/)——Cloudflare 新发布的 agent-first 云端浏览器，它的评测在 [`kitesurf-eval`](https://github.com/lexmount/Lexbench-Headless-Browser/blob/kitesurf-eval/README.zh.md) 分支。Kitesurf 目前只以远程端点的形态存在，没有二进制摘要、无法测量资源，证据等级与本地固定二进制不同（`formal_score_eligible: false`），五引擎的结果在对应的分支单独发布。

## Resource Cost

Headless browser 最大的亮点是低资源消耗，轻量引擎替代 Chrome 的论据能不能成立，另一半就看它到底省了多少，所以资源用和兼容性同样的严格程度来测。任务集取 `l1.raw_cdp`（375 道）加 `l2.web_platform`（182 道）共 557 道：资源轮必须 `--jobs 1` 串行执行，避免引擎之间互相争抢资源。所有资源统计都只在四个引擎共同通过的 task-attempt 交集（1,045 个 attempt）上计算——只有大家都完成了同一批工作，比较各自的开销才有意义；某个引擎没通过的题，不会以"便宜地失败"计入它的成绩。

由于观测行为本身可能会带来资源统计量上的误差，测量采用两轮 A/B 设计：同一批 task 先关 profiler 完整跑一轮做基线，再开 profiler 跑一轮，两轮对比量出观测行为自身的干扰，只有干扰低于校准阈值（`resource_comparison_eligible: true`）时数字才记录。

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/efficiency-map-dark.png">
    <img alt="任务成功率与单题内存峰值中位数的关系：Chrome 99.9%、697 MiB，Moli 80.7%、92 MiB，Lightpanda 43.8%、34 MiB，Obscura 39.5%、39 MiB" src="docs/assets/efficiency-map-light.png" width="100%">
  </picture>
</div>

单题进程树内存峰值的中位数：Lightpanda 34 MiB、Obscura 39 MiB、Moli 92 MiB、Chrome 697 MiB。同一批任务的单题引擎 CPU 中位数依次是 36 ms、38 ms、101 ms、687 ms。细节和校准记录见[资源卡片](docs/reports/resource-card-20260812.md)。

## Benchmark Composition

当前 bench 版本 `2026.08.17-v0_4.2`：1,928 道 task，18 个 subset，分两层。上面的已发布 run 仍固定在 `2026.08.02-v0_4.1`；当前版本只调整 task 描述与不参与执行的元数据，没有改变任务集、driver、grader、fixture 或 capability 归属。

- L1 测协议与 driver 兼容性（1,740 道）。每道题经由真实 driver 连接：裸 CDP、`playwright-core`、`puppeteer-core`、`selenium`、`chromedp`、`rod`、`chromiumoxide`、`ferrum`、`pydoll`、`cdp-use`、`chrome-remote-interface`、`chrome-devtools-mcp`、`stagehand`、`agent-browser`。其中 1,233 道由 116 个 scenario spec 跨 13 个 driver 渲染而成，同一个行为在每个生态各测一遍。
- L2 测 Web 平台语义（188 道）。覆盖 DOM、存储、网络、Worker、CSSOM，按页面最终行为判定而不是协议回声，经 capability map 归拢为 72 个 capability / 192 个计分单元。

任务集由 `manifest.json` 固定：同一个 bench 版本内任务内容冻结，任何改动都必须提升版本号。任务集会随引擎和 driver 生态的演进持续扩充，新增的 subset 和 task 以新的 bench 版本发布。

## Methodology

每次尝试都要过两道身份校验：连接前的 HTTP `/json/version` 探测，连接后在传输层上再验一次。校验不过的行记为 `infra`，不计任何分数——候选引擎底下悄悄换成 Chrome 这种情况，就是被这一步挡住的。

每条结果行都带 `pass`、`fail`、`unsupported`、`timeout`、`crash`、`infra`、`chrome_gate_fail` 之一的状态；失败的行还带 `failure.class` 和 `failure.origin`。

`docs/reports/` 下的报告由读取 run 的 `results.jsonl` 的生成器产出。同一份 run 再跑一次生成器，输出逐字节相同。

功能轮和资源轮从不共用同一次 run。资源数字来自上面那套 A/B 协议，只有 `resource_comparison_eligible` 为真时才会公布。

`run_manifest.json` 记录了 runner 源码树、fixture 树和编译后 adapter 二进制的摘要，任何人都能核对某次 run 是由哪份代码产出的。时间戳统一为 UTC，结果行不含绝对路径，`--provenance-level minimal` 保留硬件事实、去掉部署指纹。

## Documentation

| 文档 | 回答什么 |
|:---|:---|
| [docs/RUNNING.zh.md](docs/RUNNING.zh.md) | 怎么装引擎和 driver，怎么把 bench 跑起来 |
| [docs/REPRODUCE.zh.md](docs/REPRODUCE.zh.md) | 怎么复现已发布的 run，怎么重新生成报告 |
| [docs/RESULTS.zh.md](docs/RESULTS.zh.md) | 结果怎么读：判定边界、口径与适用范围 |
| [docs/reports/](docs/reports/) | 报告本身，由 run 产物生成 |

## Repository Layout

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

## License

AGPL-3.0，见 [LICENSE](LICENSE)。上游 task 与 fixture 的署名和许可信息集中保留在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
