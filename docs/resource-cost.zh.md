# 资源开销

[English](resource-cost.md) · [中文](resource-cost.zh.md)

**这一页在讲什么：** 资源遥测是功能分之外的一条**独立观测维度**。它回答"同一批成功任务，各引擎消耗了多少 CPU、内存和页面流量"，但**不改变** task 的 pass/fail、不并入原生能力分、也不生成什么"资源总分"。

目前支持 `chrome`、`moli`、`lightpanda`、`obscura` 四个引擎；聚合维度从 `run_manifest.selected_engines` 解析，不再硬编码三引擎。

## 两种运行形态

**普通 `run`** 默认开启低频采样：

```bash
python3 -m runner.run run \
  --engines chrome,moli,lightpanda,obscura \
  --host-telemetry on \
  --host-sample-interval-s 2
```

它记录：load、MemAvailable、swap、CPU/内存/IO 的 PSI（资源压力停滞指标）、benchmark 后代进程数、kernel、CPU governor 与 cgroup 版本。同时会标记 swap 活动、低可用内存、持续 PSI 或异常进程增长——但**只做标记，不改写功能结果**。确有需要时可以用 `--host-telemetry off` 关掉。

**Engine profile（引擎级资源剖析）是显式主动开启的。** 正式做资源对比时，应当用同一批任务集、seed、k 和同一台机器，先跑 profiler-off 基线，再跑 profiler-on：

```bash
# A 轮：平衡引擎顺序，但不启用 attempt profiler
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

# B 轮：同一矩阵，开启 CPU / PSS / fixture traffic 测量
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

`baseline` 与 `engine` 两种模式都按连续 task-attempt 做**严格循环轮换**（轮流分配位置）：任意 N 个 attempt 里，每个引擎占据的各个顺序位的次数最多相差 1；seed 只决定第一轮的偏移。完整扫描 Chrome 的进程树在短任务上本身可能很贵，所以 500 ms 是保守起点——应根据 A/B 结果调整，而不是机械地追求更高采样频率。

## 度量的作用域与指标

三个 scope 不混算：

- `engine_scope`：engine 根进程及其全部后代进程——所选引擎资源对比的**主对象**。
- `harness_scope`：driver、adapter、grader 等控制成本；**不计入**引擎侧的主对比。
- `host_scope`：整机环境与污染证据；只用于可比性判断和归因。

每个热启动 attempt 至少记录：

- **CPU**：`cpu_total_ms`、`cpu_user_ms`、`cpu_system_ms`、`avg_cores`。
- **PSS**（比例集大小，进程实际占用内存的合理估算）：`pss_baseline_bytes`、`pss_peak_bytes`、`pss_end_bytes`、`pss_peak_delta_bytes`。
- **进程**：baseline/peak/end 三个时点的进程数，以及 run 级 PSS/进程泄漏斜率。
- **cgroup 内存**：current/peak 作为补充统计口径上传——它不会冒充 PSS。
- **流量**：fixture 服务端收到/发出的请求响应 header 与 body 字节数。

冷启动单独写入 `cold_start.jsonl`，记录 `ready_ms`、`launch_cpu_ms`、`launch_peak_pss_bytes`，**不进入** warm 聚合。

## 底层实现与失败语义

- **CPU**：优先使用每个 worker × engine 独立的 cgroup v2 `cpu.stat`；没有委派权限时，退回 `/proc/PID/stat` 的完整进程树累计，并写 `proc_tree_child_exit_loss_risk` 标记丢失风险。
- **PSS**：对每个进程读 `/proc/PID/smaps_rollup`，按完整后代树求和。`/proc` 无法提供原子的"树 + PSS"快照，所以扫描会对短暂的子进程退出做**有界重试**。
- **僵尸进程**：已确认的 zombie 仍计入进程数，但它已释放的地址空间按 0 live PSS 处理，并写 `pss_zero_address_space_process`。
- **读失败**：权限错误或无法确认的读取失败，写 `unavailable.pss`，**绝不写成 0**。
- **枚举方式**：有独立 cgroup 时从 `cgroup.procs` 枚举完整进程集合；没有时遍历 `/proc/PID/task/TID/children`。
- **并行读取**：PSS 文件并行读取以压低观测墙钟开销，同时把各读取线程的 CPU 计入 `sampler_cpu_ms`。
- **峰值语义**：`pss_peak_bytes` 是该 attempt 所有 PSS 样本的最大值；短任务如果只有 baseline/end 两个点，会显式写 `baseline_end_only`——不能把它解释成连续采样的瞬时峰值。
- **异常隔离**：资源采集的异常被隔离在 profiler 内——task 的功能状态仍由原 driver/grader 决定，不受影响。

## Fixture 流量的统计范围

Phase 1 的可移植后端真值来自自托管的 fixture server：

- `fixture_app_rx_body_bytes`：服务器收到的 HTTP body 与 WebSocket 客户端 payload。
- `fixture_app_tx_body_bytes`：服务器发出的 HTTP/SSE body 与 WebSocket 服务端 payload。
- `fixture_app_rx_header_bytes` / `fixture_app_tx_header_bytes`：HTTP 应用层 headers。
- 重定向、HTTP 上传/下载、SSE、WebSocket 都走**同一个计数器**。
- `/__grade__/` 与 `/__event__/` 进入单独的 harness 计数，**不混入** fixture app。

`jobs=1` 时 active attempt 唯一，归属没有歧义。并发诊断 run 会尝试通过 session/referrer/cookie/body 做归属；无法唯一归属时标记 `unavailable`，**绝不伪造为 0**。portable backend 暂不实现 control-plane bytes 和 wire bytes，报告会带明确的 reason。

## 比较资格

`resource_comparison_eligible=true` 必须**同时满足**以下全部条件：

1. 至少选择两个引擎，且每条结果行的引擎都属于同一份 `selected_engines`；
2. 使用 `--resource-profile engine`、`--jobs 1`、`--k >= 5`；
3. 使用 `--score-mode independent`，且采用平衡轮换顺序；
4. 所选全部引擎在同一个 `task_id × attempt` 都 pass 的交集**非空**；
5. 交集所需的 CPU/PSS/traffic 各指标没有任何缺失；
6. host 污染门通过；
7. profiler on/off 的 A/B 完整配对：corpus、seed、k、jobs、引擎+harness pin、host provenance 完全一致；状态不一致率不超过 1%；中位任务时长与包含 baseline/end 扫描在内的采集墙钟开销，在整体和每个引擎上都不超过配置阈值。

报告只聚合"全部通过的交集"。fail/unsupported/timeout 仍保留原始资源数据，并在 `excluded.status_by_engine` 里列出——避免把"快速失败"误当成效率优势。每个指标都输出**样本数、median、p95 和 bootstrap median 95% CI**。

## 产物

普通 run 额外产生：

```text
host_telemetry.jsonl
host_summary.json
```

`--resource-profile engine` 模式下另外增加：

```text
cold_start.jsonl
resource_summary.json
resource-card.md
artifacts/<layer>/<subset>/<task>/<engine>/<attempt>/resource.jsonl
```

Attempt 摘要位于 `results.jsonl[].resource`。backend、采样间隔、样本数、sampler CPU、质量标记（quality flags）、host/kernel/governor、jobs、reuse、引擎 SHA 和 harness pin 都进入 run 溯源；`runner.source.tree_sha256` 对有效 runner 与 adapter 源文件的**实际内容**求 hash（包含未提交的修改）。

当前 schema 为：

- `abb_host_telemetry/1`
- `abb_engine_resources/1`
- `abb_fixture_traffic/1`
- `abb_resource_summary/1`
- `abb_runner_source/1`
