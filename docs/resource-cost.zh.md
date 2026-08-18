# Resource cost 与公平对比契约

[English](resource-cost.md) · [中文](resource-cost.zh.md)

Resource telemetry 是功能分之外的独立观测维度。它回答“同一批成功任务消耗了多少
CPU、内存和 fixture 应用流量”，不改变 task 的 pass/fail，不并入 native capability
分，也不生成混合“资源总分”。

当前支持 `chrome`、`moli`、`lightpanda`、`obscura`；聚合维度从
`run_manifest.selected_engines` 解析，不再硬编码三引擎。

## 两种运行形态

普通 `run` 默认开启低频 host telemetry：

```bash
python3 -m runner.run run \
  --engines chrome,moli,lightpanda,obscura \
  --host-telemetry on \
  --host-sample-interval-s 2
```

它记录 load、MemAvailable、swap、CPU/memory/IO PSI、benchmark 后代进程数、
kernel、CPU governor 与 cgroup 版本。污染门会标记 swap 活动、低可用内存、
持续 PSI 或异常进程增长，但不会改写功能结果。确有需要时可以用
`--host-telemetry off` 关闭。

Engine profile 是显式 opt-in。正式资源对比应对同一 corpus、seed、k 和机器先跑
profiler-off 基线，再跑 profiler-on：

```bash
# A：平衡引擎顺序，但不启用 attempt profiler
python3 -m runner.run run \
  --subset l1.raw_cdp \
  --tag purpose.smoke \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent \
  --jobs 1 \
  --k 5 \
  --seed resource-calibration-v1 \
  --resource-profile baseline \
  --run-id resource_ab_off

# B：同一矩阵开启 CPU/PSS/fixture traffic
python3 -m runner.run run \
  --subset l1.raw_cdp \
  --tag purpose.smoke \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent \
  --jobs 1 \
  --k 5 \
  --seed resource-calibration-v1 \
  --resource-profile engine \
  --resource-sample-interval-ms 500 \
  --resource-calibration-baseline runs/<resource_ab_off_run> \
  --resource-max-observer-effect-pct 10 \
  --run-id resource_ab_on
```

`baseline` 与 `engine` 都按连续 task-attempt 做严格循环轮换；任意 N 个 attempt
中，每个引擎占据各顺序位的次数最多相差 1，seed 只决定第一轮的偏移。
短任务上扫描完整 Chrome 进程树本身可能很贵，因此 500ms 是保守起点；应根据 A/B
结果调整，而不是机械追求更高采样频率。

## Scope 与指标

三个 scope 不混算：

- `engine_scope`：engine root 及全部 descendants，是所选引擎资源对比的主对象。
- `harness_scope`：driver、adapter、grader 等控制成本；不计入 engine 侧的主对比。
- `host_scope`：整机环境与污染证据，只用于可比性和归因。

Warm attempt 至少记录：

- CPU：`cpu_total_ms`、`cpu_user_ms`、`cpu_system_ms`、`avg_cores`。
- PSS：`pss_baseline_bytes`、`pss_peak_bytes`、`pss_end_bytes`、
  `pss_peak_delta_bytes`。
- 进程：baseline/peak/end process count，以及 run 级 PSS/process leak slope。
- cgroup memory：current/peak 作为补充 accounting；不会冒充 PSS。
- 流量：fixture application request/response headers 和 body bytes。

Cold start 单独写入 `cold_start.jsonl`，记录 `ready_ms`、`launch_cpu_ms` 和
`launch_peak_pss_bytes`，不进入 warm 聚合。

## Backend 与失败语义

CPU 优先使用每个 worker×engine 独立的 cgroup v2 `cpu.stat`。没有委派权限时，
fallback 为 `/proc/PID/stat` 的完整进程树累计，并写
`proc_tree_child_exit_loss_risk`。

PSS 使用每个进程的 `/proc/PID/smaps_rollup`，按完整后代树求和。`/proc` 无法提供
原子 tree+PSS 快照，因此扫描会对短暂的子进程退出做有界重试。已确认的 zombie
仍计入 process count，但其已释放的地址空间按 0 live PSS 处理，并写
`pss_zero_address_space_process`。权限错误或无法确认的读取失败会写
`unavailable.pss`，绝不写成 0。有独立 cgroup 时从 `cgroup.procs` 枚举完整集合，
无 cgroup 时遍历 `/proc/PID/task/TID/children`；PSS 文件并行读取以压低 observer
wall overhead，同时把各 reader thread 的 CPU 计入 `sampler_cpu_ms`。
`pss_peak_bytes` 是该 attempt 所有 PSS samples 的最大值；短任务若只有
baseline/end 两点会显式写 `baseline_end_only`，不能把它解释成连续采样的瞬时峰值。

资源采集异常被隔离在 profiler 内：task 的功能状态仍由原 driver/grader 决定。

## Fixture 流量的统计范围

Phase 1 的 portable 真值来自自托管 fixture server：

- `fixture_app_rx_body_bytes`：server 收到的 HTTP body 与 WebSocket client payload。
- `fixture_app_tx_body_bytes`：server 发出的 HTTP/SSE body 与 WebSocket server
  payload。
- `fixture_app_rx_header_bytes` / `fixture_app_tx_header_bytes`：HTTP 应用层 headers。
- redirect、HTTP upload/download、SSE、WebSocket 都走同一个计数器。
- `/__grade__/` 与 `/__event__/` 进入单独的 harness 计数，不混入 fixture app。

`jobs=1` 时 active attempt 唯一，归属无歧义。并发 diagnostic run 会尝试通过
session/referrer/cookie/body 归属；无法唯一归属时标记 unavailable，绝不伪造为
0。portable backend 暂不实现 control-plane bytes 和 wire bytes，报告会带明确
reason。

## 比较资格

`resource_comparison_eligible=true` 必须同时满足：

1. 至少选择两个 engine，且每个 result row 的 engine 都属于同一份
   `selected_engines`；
2. `--resource-profile engine`、`--jobs 1`、`--k >= 5`；
3. `--score-mode independent`，且使用平衡轮换顺序；
4. 所选全部引擎在同一个 `task_id × attempt` 都 pass 的交集非空；
5. 交集所需 CPU/PSS/traffic 指标无缺失；
6. host pollution gate 通过；
7. profiler on/off A/B 有完整配对，corpus/seed/k/jobs/engine+harness pin/host
   provenance 完全一致，状态不一致率不超过 1%，中位 task duration 与包含
   baseline/end 扫描在内的 collection-wall 开销在整体和每个引擎上都不超过
   配置阈值。

报告只聚合 all-pass intersection。fail/unsupported/timeout 仍保留原始资源数据，
并在 `excluded.status_by_engine` 中列出，避免把 fail-fast 当成效率优势。每个指标
输出样本数、median、p95 和 bootstrap median 95% CI。

## 产物

普通 run 增加：

```text
host_telemetry.jsonl
host_summary.json
```

`--resource-profile engine` 另外增加：

```text
cold_start.jsonl
resource_summary.json
resource-card.md
artifacts/<layer>/<subset>/<task>/<engine>/<attempt>/resource.jsonl
```

Attempt summary 位于 `results.jsonl[].resource`。backend、采样间隔、样本数、
sampler CPU、quality flags、host/kernel/governor、jobs、reuse、engine SHA 和
harness pin 都进入 run provenance；`runner.source.tree_sha256` 对有效 runner 与
adapter 源文件的实际内容求 hash，包含未提交修改。当前 schema 为：

- `abb_host_telemetry/1`
- `abb_engine_resources/1`
- `abb_fixture_traffic/1`
- `abb_resource_summary/1`
- `abb_runner_source/1`


