<p align="center">
  <img src="docs/assets/lexbench-hero.png" alt="Lexbench-Headless-Browser — Benchmarking headless browsers as agent runtimes" width="100%">
</p>

<h1 align="center">Lexbench-Headless-Browser</h1>

<p align="center">
  <a href="https://x.com/LexmountAI"><img src="https://img.shields.io/badge/X-%40LexmountAI-000000?style=flat&logo=x&logoColor=white" alt="在 X 上关注 @LexmountAI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache--2.0-2F80ED.svg" alt="许可证：Apache-2.0"></a>
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

你现在在 `kitesurf-eval`，也就是 Cloudflare 新发布的 agent-first 云端浏览器 [Kitesurf](https://blog.cloudflare.com/kitesurf/) 的评测分支。完整的项目介绍与动机在 [`main`](https://github.com/lexmount/Lexbench-Headless-Browser/blob/main/README.zh.md) 分支：这个 benchmark 为什么存在、四个本地固定二进制引擎的排行榜，以及资源测量。Kitesurf 目前只以远程端点的形态存在，没有二进制摘要，也没有可测量的进程树，证据等级与本地二进制不同（`formal_score_eligible: false`）。这个分支加上第五列、让远程引擎变得可测的那套机制，并在两个明确定义的任务子集上发布对比结果。其余一切（1,928 道任务集、driver、grader 和方法）与 `main` 完全一致。

## Results

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/five-engine-caliber-b-dark.png">
    <img alt="五个无头浏览器在 1,308 道可比任务上的成功率：Chrome 99.8%、Moli 81.9%、Kitesurf 62.1%、Lightpanda 53.3%、Obscura 44.9%" src="docs/assets/five-engine-caliber-b-light.png" width="100%">
  </picture>
</div>

<div align="center">

| 引擎 | 任务子集 A（1,671 道） | 任务子集 B（1,308 道） |
|:---|---:|---:|
| [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) | 1,669 / 1,671 · **99.88%** | 1,306 / 1,308 · **99.85%** |
| [Moli](https://github.com/lexmount/moli) | 1,359 / 1,671 · **81.33%** | 1,071 / 1,308 · **81.88%** |
| [Kitesurf](https://blog.cloudflare.com/kitesurf/)（远程） | 812 / 1,671 · **48.59%** | 812 / 1,308 · **62.08%** |
| [Lightpanda](https://github.com/lightpanda-io/browser) | 697 / 1,671 · **41.71%** | 697 / 1,308 · **53.29%** |
| [Obscura](https://github.com/h4ckf0r0day/obscura) | 660 / 1,671 · **39.50%** | 587 / 1,308 · **44.88%** |

</div>

两个任务子集都从同一份 1,928 道的任务集出发，剔除的题对五个引擎一视同仁。任务子集 A 去掉三个 subset 共 257 道题，因为面对远程端点时失败无法归因，剩下的就是可归因面。任务子集 B 在此基础上再去掉四个 subset 共 363 道题，它们整列为零且各自有单一的系统性根因，剩下的就是五列可以直接对比的集合。Kitesurf 这条 lane 以 k=1 加 B 类裁定运行，覆盖了任务子集 A 里的 1,305 道和任务子集 B 里的 1,302 道，未覆盖的一律按未通过计。完整报告见 [docs/reports/five-engine-report-20260813.md](docs/reports/five-engine-report-20260813.md)（报告中两个任务子集记作 Caliber A/B）。

全任务集上的四引擎排行榜（每个引擎都是本地固定二进制、不需要任何剔除的那份）在 [`main`](https://github.com/lexmount/Lexbench-Headless-Browser/blob/main/README.zh.md#results)。

## Resource Cost

资源数字只覆盖四个本地引擎。Kitesurf 跑在这套框架并不拥有的共享基础设施上，所以它的 CPU、内存和进程数是无法测量，而不是零；空着的格子也绝不代表便宜。

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/efficiency-map-dark.png">
    <img alt="四个本地引擎的任务成功率与单题内存峰值中位数：Chrome 99.9%、697 MiB，Moli 80.7%、92 MiB，Lightpanda 43.8%、34 MiB，Obscura 39.5%、39 MiB" src="docs/assets/efficiency-map-light.png" width="100%">
  </picture>
</div>

单题进程树内存峰值的中位数：Lightpanda 34 MiB、Obscura 39 MiB、Moli 92 MiB、Chrome 697 MiB。同一批任务的单题引擎 CPU 中位数依次是 36 ms、38 ms、101 ms、687 ms。细节和校准记录见[资源卡片](docs/reports/resource-card-20260812.md)。

## Documentation

<div align="center">

| 文档 | 回答什么 |
|:---|:---|
| [docs/kitesurf-deployment.zh.md](docs/kitesurf-deployment.zh.md) | 怎么部署 fixture，怎么跑 Kitesurf lane |
| [docs/EVIDENCE.zh.md](docs/EVIDENCE.zh.md) | 证据链：run 的指纹文件、Release 压缩包，以及重新生成报告的命令 |
| [docs/RUNNING.zh.md](docs/RUNNING.zh.md) | 怎么装引擎和 driver，怎么把 bench 跑起来 |
| [docs/REPRODUCE.zh.md](docs/REPRODUCE.zh.md) | 怎么复现已发布的 run，怎么重新生成报告 |
| [docs/RESULTS.zh.md](docs/RESULTS.zh.md) | 结果怎么读：判定边界与适用范围 |
| [docs/reports/](docs/reports/) | 报告本身，由 run 产物生成 |

</div>

## 这个分支多了什么

- runner 和各 adapter 里通用的 `remote_cdp` 身份契约，采用同一连接上的三字段校验，让远程端点也要过和本地二进制一样的防偷换关卡。见 [runner/scripts/adapters/PROTOCOL.md](runner/scripts/adapters/PROTOCOL.md)。
- 配方机制：`tools/kitesurf_experiments.py {check,list,render,run}`，作用于 [config/kitesurf_experiments.json](config/kitesurf_experiments.json)。
- 双 origin 的 fixture 契约：静态 fixture 由本仓库的 GitHub Pages 提供（`pages/`，由 [config/kitesurf_static_fixture.json](config/kitesurf_static_fixture.json) 固定），动态 origin 则自行部署（`python3 -m runner.run fixture-serve` 加一条 HTTPS 隧道，由 [config/kitesurf_dynamic_fixture.json](config/kitesurf_dynamic_fixture.json) 做契约校验）。
- 五引擎聚合：`tools/report_five_engine.py` 直接计算已发布的分母、任务子集划分和 B 类裁定，而不是用文字描述它们。

先读 [docs/kitesurf-deployment.zh.md](docs/kitesurf-deployment.zh.md)，里面是 fixture 的解析规则和四条命令的流程。等 Kitesurf 提供本地二进制，它就并入主名单，这个分支随之退役。

## License

Apache-2.0，见 [LICENSE](LICENSE)。上游 task 与 fixture 的署名和许可信息集中保留在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
