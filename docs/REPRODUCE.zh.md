# 复现已发布的 run

[English](REPRODUCE.md) · [中文](REPRODUCE.zh.md)

报告里的每一个数字，都能追溯到下面三次 run 之一。这一页记录它们的精确参数、一次 run 怎么变成一份报告，以及你的复现结果对不上时该查什么。

这个分支在此之上多发布一个结果：Kitesurf lane 和它喂出来的五引擎报告。采集流程见 [kitesurf-deployment.zh.md](kitesurf-deployment.zh.md)，重新生成五引擎报告的命令见 [EVIDENCE.zh.md](EVIDENCE.zh.md)。

## 三次已发布的 run

| Run id | 用途 | 任务集 | k | jobs | Profiler |
|:---|:---|:---|---:|---:|:---|
| `four_engine_full_20260812` | 功能分 | 全量 1,928 道 | 3 | 16 | 关 |
| `resource_baseline_20260812` | 资源 A 轮 | `l1.raw_cdp` + `l2.web_platform`，557 道 | 5 | 1 | 关 |
| `resource_engine_20260812` | 资源 B 轮 | 同一批 557 道 | 5 | 1 | 开 |

三次 run 的共同参数：bench 版本 `2026.08.02-v0_4.1`（按当前方案即数据集 `0.4.1`，见[版本号](RESULTS.zh.md#版本号)）、seed `official20260709`、`--score-mode independent --chrome-baseline best_effort`、引擎 `chrome,moli,lightpanda,obscura`。每个引擎的 pin（版本与 sha256）列在各报告最后的溯源表里，由 `doctor` 强制校验；二进制的获取位置——[Moli](https://github.com/lexmount/moli)、[Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/)、[Lightpanda](https://github.com/lightpanda-io/browser)、[Obscura](https://github.com/h4ckf0r0day/obscura)——与放置路径见 [RUNNING.zh.md](RUNNING.zh.md)。下面的命令逐字取自各 run 的 `run_manifest.json` 里记录的 `argv`（实际启动参数）。

**功能 run：**

```bash
python3 -m runner.run run \
  --engines chrome,moli,lightpanda,obscura \
  --chrome-baseline best_effort --score-mode independent \
  --seed official20260709 --k 3 --jobs 16 --host-telemetry on \
  --run-id four_engine_full_20260812 --provenance-level minimal
```

**资源 run**（必须按顺序执行，B 轮要引用 A 轮）：

```bash
python3 -m runner.run run \
  --subset l1.raw_cdp --subset l2.web_platform \
  --engines chrome,moli,lightpanda,obscura \
  --chrome-baseline best_effort --score-mode independent \
  --seed official20260709 --k 5 --jobs 1 --host-telemetry on \
  --resource-profile baseline \
  --run-id resource_baseline_20260812 --provenance-level full

python3 -m runner.run run \
  --subset l1.raw_cdp --subset l2.web_platform \
  --engines chrome,moli,lightpanda,obscura \
  --chrome-baseline best_effort --score-mode independent \
  --seed official20260709 --k 5 --jobs 1 --host-telemetry on \
  --resource-profile engine \
  --resource-calibration-baseline runs/resource_baseline_20260812 \
  --resource-max-observer-effect-pct 20 \
  --run-id resource_engine_20260812 --provenance-level full
```

为什么观测干扰阈值用 `20` 而不是默认的 `10`：Chrome 的进程树超过一百个进程，每次尝试首尾各做一次全树 PSS 扫描，本身就是测量负担带来的固有成本。报告里记录了这个选择，并给出实际的任务时长干扰 ≤0.87%。

**注意：** 资源两轮必须在**同一台机器**上跑，且中途不能有别的负载。只有两轮之间机器状态保持不变，A/B 对比才有意义。

## 从 run 到报告

报告是**生成**出来的，不是手写出来的：

```bash
python3 tools/report_four_engine.py runs/four_engine_full_20260812 \
  -o docs/reports/four-engine-report-20260812.md
```

生成器读 `results.jsonl` 并重新计算一切。对同一个 run 目录跑两遍，输出**逐字节相同**——这也是验证已发布报告没被手改过的方法：重新生成，然后 `diff`。

## 复现凭什么可比

- `--run-id` 逐字命名 run 目录，所有记录的时间戳都是 UTC。
- 每次尝试的随机种子由 `sha256(seed:task_id:attempt)` 推导，与引擎顺序和壁钟时间无关。
- `run_manifest.json` 对 runner 源码树、fixture 树和编译后的 adapter 二进制做了摘要。对比两次 run 之前，先对比这些摘要：摘要不同，说明框架本身不同。
- 结果行不含任何主机绝对路径；每次启动的临时目录记录为 `<ephemeral>`。
- `--provenance-level minimal` 保留硬件与内核事实、去掉 cgroup 路径和 CPU 亲和性这类部署指纹。资源两轮用的是 `full`，因为校准审计需要部署细节；功能轮以 `minimal` 发布。

## 证据放在哪

run 目录不入库（`runs/` 在 gitignore 里；三次已发布 run 未压缩合计超过 1 GB）。已发布的证据以压缩包形式挂在仓库的 GitHub Releases 上：每个包内含 `results.jsonl`、`run_manifest.json` 和摘要文件，sha256 校验和随包列出。克隆仓库得到的是框架和报告；要审计或重新生成结果时，再去取证据包。

## 你的数字对不上时

在得出任何关于浏览器的结论之前，先沿这架梯子往下查：

1. 对比 `run_manifest.json` 的摘要和引擎 sha256 与已发布值。**pin 不同意味着你测的是不同的软件**——这是一个答案，不是一个错误。
2. 确认 `doctor` 是绿的，并且结果里没有异常数量的 `infra`。身份失败是环境问题。
3. 功能分层面，flake 级的小差异表现为某些题只过了 3 次中的 2 次；"所有尝试都通过才算通过"的规则让头条数字对真实的不稳定敏感，这是有意的设计。
4. 资源数字层面，确认你自己的 B 轮里 `resource_comparison_eligible: true`。没过校准门的轮次产出的数字，跟任何人的都不可比（包括我们的）。资源数字也永远**不外推**到 557 道之外。
