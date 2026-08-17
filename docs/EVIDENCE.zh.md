# 评测证据

[English](EVIDENCE.md) · [中文](EVIDENCE.zh.md)

`docs/reports/` 里的每个数字都来自某次 run 自己的产物。这页是两者之间的索引：哪些
文件固定了一次 run 的身份，哪些压缩包装着它的原始结果行，以及每个包的 sha256 应该
是多少。

链条是 报告 → 本索引 → Release 资产。小文件随仓库分发，大产物不进 git 历史。

## 随仓库分发的部分

`docs/evidence/<run_id>/` 放的是给一次 run 打指纹的文件，合计约 4.7 MB，不下载任何
东西就能核对一个已发布的数字是由什么产出的。

| 文件 | 固定了什么 |
|:---|:---|
| `run_manifest.json` | 引擎版本与 sha256、各生态的 driver pin、runner 源码树 / fixture 树 / 每个编译 adapter 的摘要、seed，以及完整 argv |
| `scores.json` | 按评测轴和 subset 的计数、`score_eligible`、failure class 与 failure origin 的统计 |
| `host_summary.json` | 主机遥测的时间窗、采样数,以及机器当时是否被抢占（`polluted`） |
| `resource_summary.json` | 校准后的资源分布、observer-effect 的 A/B 对比,以及 `resource_comparison_eligible`（仅资源轮） |
| `cold_start.jsonl` | 冷启动诊断，与 warm 画像分开保存（仅资源轮） |

报告里有两张表可以直接对着这些文件核。四引擎的评测轴表里 Chrome 在 L1 是
5214/5220、L2 是 192/192，就是 `scores.json` 里的值；资源卡片里 Chrome 的 CPU 中位数
687 ms，对应 `by_engine.chrome.metrics.cpu_total_ms.median` 的 686.948。

头条的任务级通过率需要结果行本身，因为一道题只有所有尝试都通过才算通过。那些行在下
面的压缩包里。

## Release 资产

挂在 [v0.5.0](https://github.com/lexmount/Lexbench-Headless-Browser/releases/tag/v0.5.0)
上。每个 `evidence-*` 包含一次 run 的 `results.jsonl`、`host_telemetry.jsonl` 和
`scorecard.md`；每个 `artifacts-*` 包含该次 run 每个 attempt 的原始协议日志，第三方
因此可以审计某一次失败的协议交互，而不只是看结果行。

| 资产 | 内容 | 下载 | 解压后 |
|:---|:---|---:|---:|
| `evidence-four_engine_full_20260812.tar.gz` | 23,136 条结果行、主机遥测、scorecard | 2.1 MiB | 46 MiB |
| `evidence-resource_baseline_20260812.tar.gz` | 11,140 行，profiler 关 | 610 KiB | 24 MiB |
| `evidence-resource_engine_20260812.tar.gz` | 11,140 行，profiler 开，另含生成的资源卡片 | 1.6 MiB | 41 MiB |
| `artifacts-four_engine_full_20260812.tar.gz` | 133,882 个 attempt 级产物文件 | 15.1 MiB | 173 MiB |
| `artifacts-resource_baseline_20260812.tar.gz` | 59,366 个文件 | 7.1 MiB | 111 MiB |
| `artifacts-resource_engine_20260812.tar.gz` | 70,505 个文件 | 13.1 MiB | 171 MiB |

Kitesurf lane 另有两个资产挂在同一个 Release 上，由
[`kitesurf-eval` 分支的 `docs/EVIDENCE.md`](../../tree/kitesurf-eval/docs/EVIDENCE.md)
索引。

### sha256

```
3052461b458581c8da620d55c3741d18dc50693d78c7a6ebeffb2241251f12f9  evidence-four_engine_full_20260812.tar.gz
55d4f98f4f386ec2f7856d0b96b2c8f9e9838d872f00f03f1589e6e8a94f51a5  evidence-resource_baseline_20260812.tar.gz
935c6232e5598cddd25cebc11ed2c35e03e43b0d3f77c1bc011787f07c0b8162  evidence-resource_engine_20260812.tar.gz
c6680f481c8d928373cb627534d6cec02754b2126ead81e2eded5ef3ab2e53cc  artifacts-four_engine_full_20260812.tar.gz
dbf474352f8db054cf61870bc8b0792f8b1d48aa38b8e4ee2a31b337e8bd0ab9  artifacts-resource_baseline_20260812.tar.gz
6c67855929d185d1a41fc59920d32c62e459a15fd2719c86f611582f8b83b349  artifacts-resource_engine_20260812.tar.gz
```

核对下载：

```bash
sha256sum -c <<'EOF'
3052461b458581c8da620d55c3741d18dc50693d78c7a6ebeffb2241251f12f9  evidence-four_engine_full_20260812.tar.gz
EOF
```

打包时固定了成员顺序、归属和 mtime，所以同一次 run 重新打包能得到相同的 sha256。

## 重新生成报告

把某次 run 的 `evidence-*` 解压到它的 `docs/evidence/<run_id>/` 旁边，让生成器同时看
到结果行和 manifest，然后：

```bash
python3 tools/report_four_engine.py runs/four_engine_full_20260812 \
    -o docs/reports/four-engine-report-20260812.md
```

两份已发布的报告都用这种方式从 Release 压缩包里重新生成过，输出与仓库里的副本逐字节
相同。五引擎报告在 `kitesurf-eval` 分支生成，它的输入在那边。

## 发布前去掉了什么

Run 产物会记录这一轮实际用过的路径和 origin，这是本地审计线索而不是证据。
`tools/scrub_release_paths.py` 在打包时改写三类信息：

| 信息 | 改写为 |
|:---|:---|
| 产出这次 run 的 checkout 的绝对路径 | `<repo>` |
| 这一轮 fixture 由哪个 host 提供 | `<static-fixture-origin>` 或 `<dynamic-fixture-origin>` |
| 带非 UTC 偏移的 ISO-8601 时间戳 | 同一时刻的 `+00:00` 表示 |

fixture origin 是从产物自身发现的：verification 报告里的 `base_url`、run summary 里
的 `scope.fixture_base_url`，所以这个脚本本身不写死任何 host。

指纹链一律不动：引擎和 adapter 的 sha256 保持原样，只有"通向某个二进制的路径"被当作
本地信息处理。四引擎那次 run 改写了 1,338 个文件里的 5,028 处路径，origin 改写为
零，因为它的 fixture 由 `127.0.0.1` 提供。清洗后的树重新生成两份报告仍然逐字节相同，
这就是"改写没有动到证据"的验证方式。

随时复查一棵树：

```bash
python3 tools/scrub_release_paths.py runs/ --check
python3 tools/scrub_release_paths.py build/release-runs/ --check --origins-from runs/
```

第二种形式用来检查已经清洗过的树。origin 是从产物里发现的，而清洗过的产物已经不再
写出任何 origin，所以这种检查需要原始的树来告诉它要找什么。如果让脚本在没有这个参数
的情况下检查一棵已清洗的树，它会拒绝执行，而不是给出一个根本没验过的"干净"结论。
