# 评测证据

[English](EVIDENCE.md) · [中文](EVIDENCE.zh.md)

`docs/reports/` 里的每个数字，都来自某次 run 自己留下的产物。这一页就是"报告 ↔ 产物 ↔ 下载包"三者之间的索引：哪些文件固定了一次 run 的身份（给一次 run 打上指纹）、哪些压缩包装着它的原始结果行、每个包的 sha256 校验和应该是多少。

证据链条是：**报告 → 本索引 → Release 发布资产**。小文件跟着仓库一起分发，大产物不进 git 历史。

## 随仓库分发的部分

`docs/evidence/<run_id>/` 放的是给一次 run 打指纹的文件，合计约 **4.7 MB**。不看报告、不下载任何东西，光凭这些文件就能核对"某个已发布数字到底是由什么产出的"。

| 文件 | 固定了什么 |
|:---|:---|
| `run_manifest.json` | 引擎版本与 sha256、各生态的 driver pin、runner 源码树 / fixture 树 / 每个编译 adapter 的摘要、seed，以及完整的启动参数 |
| `scores.json` | 按评测维度和子集的计数、`score_eligible`、failure class 与 failure origin 的统计 |
| `host_summary.json` | 主机遥测的时间窗、采样数，以及机器当时是否被抢占（`polluted`） |
| `resource_summary.json` | 校准后的资源分布、观测者效应的 A/B 对比，以及 `resource_comparison_eligible`（仅资源轮） |
| `cold_start.jsonl` | 冷启动诊断，与热启动画像分开保存（仅资源轮） |

报告里有两张表可以直接对着这些文件核验。例如：四引擎报告的评测轴表里，Chrome 在 L1 是 5214/5220、L2 是 192/192，就是 `scores.json` 里的原值；资源卡片里 Chrome 的 CPU 中位数 687 ms，对应 `by_engine.chrome.metrics.cpu_total_ms.median` 里的 686.948。

头条的"任务级通过率"需要原始结果行才能算——因为一道题要**所有尝试都通过**才算通过，而这个信息只存在下面的压缩包里。

## 发布资产

挂在 [evidence-20260812](https://github.com/lexmount/Lexbench-Headless-Browser/releases/tag/evidence-20260812) 这个 Release tag 上。注意：这个 tag 是按**采集时间**而不是代码状态命名的——这批 run 采集于 2026-08-12 和 08-13，而 tag 指向的那棵树比它们更晚。

每个 `evidence-*` 包包含某次 run 的 `results.jsonl`、`host_telemetry.jsonl` 和 `scorecard.md`；每个 `artifacts-*` 包则包含该次 run 每一次尝试的**原始协议日志**——第三方因此可以审计某一次失败到底跟浏览器做了什么交互，而不只是看结果行。

| 资产 | 内容 | 下载 | 解压后 |
|:---|:---|---:|---:|
| `evidence-four_engine_full_20260812.tar.gz` | 23,136 条结果行、主机遥测、scorecard | 2.1 MiB | 46 MiB |
| `evidence-resource_baseline_20260812.tar.gz` | 11,140 行，profiler 关 | 610 KiB | 24 MiB |
| `evidence-resource_engine_20260812.tar.gz` | 11,140 行，profiler 开，另含生成的资源卡片 | 1.6 MiB | 41 MiB |
| `artifacts-four_engine_full_20260812.tar.gz` | 133,882 个 attempt 级产物文件 | 15.1 MiB | 173 MiB |
| `artifacts-resource_baseline_20260812.tar.gz` | 59,366 个文件 | 7.1 MiB | 111 MiB |
| `artifacts-resource_engine_20260812.tar.gz` | 70,505 个文件 | 13.1 MiB | 171 MiB |

Kitesurf lane 另有两个资产挂在同一个 Release 上，由下方 [Kitesurf lane](#kitesurf-lane) 一节索引。

### sha256 校验和

```text
3052461b458581c8da620d55c3741d18dc50693d78c7a6ebeffb2241251f12f9  evidence-four_engine_full_20260812.tar.gz
55d4f98f4f386ec2f7856d0b96b2c8f9e9838d872f00f03f1589e6e8a94f51a5  evidence-resource_baseline_20260812.tar.gz
935c6232e5598cddd25cebc11ed2c35e03e43b0d3f77c1bc011787f07c0b8162  evidence-resource_engine_20260812.tar.gz
c6680f481c8d928373cb627534d6cec02754b2126ead81e2eded5ef3ab2e53cc  artifacts-four_engine_full_20260812.tar.gz
dbf474352f8db054cf61870bc8b0792f8b1d48aa38b8e4ee2a31b337e8bd0ab9  artifacts-resource_baseline_20260812.tar.gz
6c67855929d185d1a41fc59920d32c62e459a15fd2719c86f611582f8b83b349  artifacts-resource_engine_20260812.tar.gz
```

核对已下载的文件：

```bash
sha256sum -c <<'EOF'
3052461b458581c8da620d55c3741d18dc50693d78c7a6ebeffb2241251f12f9  evidence-four_engine_full_20260812.tar.gz
EOF
```

打包时固定了成员的顺序、归属和 mtime（修改时间），所以同一份 run 重新打包能得到相同的 sha256——指纹可复现。

## 重新生成报告

把某次 run 的 `evidence-*` 包解压到它自己的 `docs/evidence/<run_id>/` 旁边，让生成器同时看到结果行和 manifest，然后跑：

```bash
python3 tools/report_four_engine.py runs/four_engine_full_20260812 \
    -o docs/reports/four-engine-report-20260812.md
```

两份已发布报告都用这种方式从 Release 压缩包重新生成过，输出与仓库里的副本**逐字节相同**。五引擎报告的重新生成命令见下方 Kitesurf lane 一节。

## 发布前去掉了什么

Run 产物会记录这一轮实际用过的路径和 origin（来源主机）——这些是**本地审计线索，不是证据**。`tools/scrub_release_paths.py` 会在打包时改写三类信息：

| 信息 | 改写为 |
|:---|:---|
| 产出这次 run 的 checkout 的绝对路径 | `<repo>` |
| 这一轮 fixture 由哪个 host 提供 | `<static-fixture-origin>` 或 `<dynamic-fixture-origin>` |
| 带非 UTC 偏移的 ISO-8601 时间戳 | 同一时刻的 `+00:00` 表示 |

fixture origin 是从产物自身发现的——verification 报告里的 `base_url`、run summary 里的 `scope.fixture_base_url`——所以这个脚本本身不写死任何 host。

指纹链一律不动：引擎和 adapter 的 sha256 保持原样，只有"指向某个二进制的路径"被当作本地信息处理。四引擎那次 run 一共改写了 **1,338 个文件里的 5,028 处路径**；origin 改写为零，因为它的 fixture 由 `127.0.0.1` 提供。清洗后的树重新生成两份报告仍然逐字节相同——这就是"改写没有动到证据"的验证方式。

随时复查一棵树：

```bash
python3 tools/scrub_release_paths.py runs/ --check
python3 tools/scrub_release_paths.py build/release-runs/ --check --origins-from runs/
```

第二种形式用来检查**已经清洗过的**树。origin 是从产物里发现的，而清洗过的产物已经不再写出任何 origin，所以这种检查需要原始的树来告诉它"要去找什么"。如果让脚本在没有 `--origins-from` 的情况下检查一棵已清洗的树，它会**直接拒绝执行**——而不是给出一个根本没验过的"干净"结论。

## Kitesurf lane

五引擎报告有两个来源：四个本地引擎来自上文索引的那次四引擎 run，Kitesurf 那一列来自本分支采集的 lane。

### 随仓库分发的部分

`docs/evidence/<run_id>/` 放着这条 lane 全部 49 轮的指纹文件，合计约 1.2 MB。

| 文件 | 固定了什么 |
|:---|:---|
| `provenance.json` | 分支、HEAD、树摘要、worktree 状态，以及该轮调用过的每个编译 adapter 的 sha256 |
| `summary.json` | 端点、期望与实测身份、状态统计、延迟，以及该轮所依赖的 fixture 校验结果 |
| `summary.md` | 同一轮的可读版本 |
| `identity.cdp.jsonl` | 传输层上的实时身份交换，每条连接一行 |

旁边还有两份 lane 级的契约报告：`ks_static_verification.json`（19 个静态 fixture 文件
对内容契约校验通过）和 `ks_dynamic_verification.json`（开跑前校验 127 条静态路由加 28
个动态探针）。

每一轮记录的源码身份都相同,说明 49 轮出自同一个未改动的工作状态。这个身份是一个
commit 和 tree,来自本仓库为公开发布而重建历史之前,所以 `provenance.json` 里那两个
哈希在这里已经解析不出对象了 —— 它们的作用是内部一致性记录,而不是可以 checkout 的
坐标。

### Release 资产

挂在 [evidence-20260812](https://github.com/lexmount/Lexbench-Headless-Browser/releases/tag/evidence-20260812)，
与四引擎的压缩包放在一起。

| 资产 | 内容 | 下载 | 解压后 |
|:---|:---|---:|---:|
| `evidence-kitesurf_lane_20260813.tar.gz` | 全部 49 轮：`results.jsonl`、每轮的 `fixture_verification.json`，以及上表那些指纹文件 | 1.2 MiB | 13 MiB |
| `artifacts-kitesurf_lane_20260813.tar.gz` | 4,989 个 attempt 级协议产物 | 888 KiB | 12 MiB |

```
003b4af6eb7ccecfd13e7bf09cfb7d00db95c0e5d40338629b8663efa4fb5fd8  evidence-kitesurf_lane_20260813.tar.gz
71b55f610b5b2f40742c43297a6f4e44a6baceec70f228ec7556eec3175a4a75  artifacts-kitesurf_lane_20260813.tar.gz
```

四引擎的压缩包，以及同样适用于这条 lane 的清洗规则，都在上文各节里索引。

### 重新生成五引擎报告

这条 lane 是 49 轮而不是一次 run：只要某道题无法确认 target 清理干净，探针就会整体触
发熔断，外层 wrapper 再对剩下的题起一次续跑，playwright 一个栈就被切成了 25 段。每
一段都是一个输入，每次失败的 rerun 证据是另一类输入：

```bash
python3 tools/report_five_engine.py \
    --four-engine-run ../main/runs/four_engine_full_20260812 \
    --kitesurf-results runs/ks_raw_full/results.jsonl \
                       runs/ks_driver_full/results.jsonl \
                       runs/ks_l2_full/results.jsonl \
                       runs/ks_blocked_*/results.jsonl \
                       runs/ks_sweep_*/results.jsonl \
    --kitesurf-rerun runs/ks_adj_*/results.jsonl \
                     runs/ks_l2_full/results.jsonl \
    -o docs/reports/five-engine-report-20260813.md
```

`ks_l2_full` 在两边都出现，是因为那一轮每道题带两行：它自己的首轮尝试和它自己的
rerun。

跑这条命令能把已发布报告的全部生成段落逐字节复现，用清洗后的压缩包和用原始树都一
样。报告末尾的"Run notes"是生成之后手写的散文段落，讲方法和观察，引用的数字都来自它
上面那些生成段落已经算出来的值。

### Kitesurf 这一列不是什么

远程端点没有二进制摘要、没有进程树、没有 cgroup，所以这条 lane 的
`formal_score_eligible` 为假，也不存在属于它的资源数字。资源格子空着表示无法测量，
绝不表示零。这些读数是对一个可能随时变化的服务的一次快照，可复现性等级弱于上文那四个固定二进制。
