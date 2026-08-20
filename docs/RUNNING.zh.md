# 把评测跑起来

[English](RUNNING.md) · [中文](RUNNING.zh.md)

这篇文档教你**在自己机器上完整跑一轮评测（run）**。如果你的目标是精确复现已发布的数字，看完这页之后，接着读 [REPRODUCE.zh.md](REPRODUCE.zh.md)。

## 测哪些浏览器

| 引擎 | 角色 | 二进制从哪来 |
|:---|:---|:---|
| [Moli](https://github.com/lexmount/moli) | 候选 | 仓库 Releases，或按其构建文档自行编译 |
| [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) | 参照列 | 官方版本页下载（chromedriver 同页配套） |
| [Lightpanda](https://github.com/lightpanda-io/browser) | 候选 | 仓库 Releases，或按其构建文档自行编译 |
| [Obscura](https://github.com/h4ckf0r0day/obscura) | 候选 | 仓库 Releases，或按其构建文档自行编译 |

角色列的含义：**候选** = 被测对象（这套题要检验的引擎）；**参照列** = 标准答案（Chrome，不参与“替代”比较，只当“题能不能过”的底线和资源消耗的尺子）。

`--engines` 参数接受上面四个名字的任意逗号组合，默认全选。注意：**chromedriver 不是被测对象**，它只是 Selenium 连浏览器时需要的桥。远程引擎 Kitesurf 不走 `--engines`：它那条 lane 由配方机制驱动，见 [kitesurf-deployment.zh.md](kitesurf-deployment.zh.md)。

## 最短路径：先跑通一个引擎

只想最快看到一个引擎动起来？你不需要准备四个二进制，也不需要 Go/Rust/Ruby 工具链（它们只服务于对应的编译型 adapter 子集）。只要：放好一个引擎的二进制 → 装上 Node 依赖 → 在裸 CDP 子集上跑一遍冒烟测试（smoke，快速验证链路能不能通）：

```bash
npm ci
python3 -m runner.run run --subset l1.raw_cdp --tag purpose.smoke \
  --engines chrome --score-mode independent --seed smoke
```

把 `chrome` 换成 `moli`、`lightpanda` 或 `obscura`，就是测别的引擎。

**注意边界：** 单引擎 run 照常把结果写进 `results.jsonl`（每道题的 pass/fail 都能看），但**正式计分要求完整的引擎名单**——只测部分引擎的 run，一律标 `score_included: false`。这种跑法用来验证环境和观察单个引擎，**不能用来对外报数**。

## 前提

- 操作系统：**Linux**（暂不支持其他平台）。
- 需要 **cgroup v2**：资源遥测要读 cgroup 和进程树来统计用量（cgroup 是 Linux 的资源统计机制）。
- 需要 **Python 3.11+** 和 **Node 20**。
- Go、Rust、Ruby 只有编译型 adapter 才用得到：`chromedp` 和 `rod`（Go）、`chromiumoxide`（Rust）、`ferrum`（Ruby）。

## 1. 引擎二进制

引擎二进制**不入库**（不放进仓库），按上面「二进制从哪来」一列获取。**要复现已发布数字时，版本必须与报告溯源表里锁定的 pin（版本 + sha256 指纹）完全一致。**

把一组固定版本的二进制放到 `build_artifacts/sets/<name>/` 下，附上 `set.json` 清单，然后激活这一组：

```bash
tools/select_engine_set.sh <name>
```

激活后的约定路径：

```
build_artifacts/chrome-for-testing/bin/chrome
build_artifacts/moli/bin/moli
build_artifacts/lightpanda/zig-out/bin/lightpanda
build_artifacts/obscura/bin/obscura          # 标准构建，非 stealth
build_artifacts/chromedriver/bin/chromedriver
```

`runner/run.py` 里的 `ENGINE_DEFS` 记录每台引擎的证据 pin（版本 + sha256）；`build_artifacts/active-set.json` 可以在单台机器上覆盖默认设置。**任何 run 之前，`doctor` 都会拿 pin 逐个核对二进制**——放错或动过手脚的二进制会大声报错，而不是默默跑出一串数字。

## 2. driver 依赖

```bash
npm ci                              # Node 侧 driver，由 package-lock.json 固定版本
pip install -e '.[drivers,dev]'     # selenium + pydoll 的 pin，以及 pytest
gem install ferrum -v 0.17.2
go build -C runner/scripts/adapters/chromedp_adapter -o chromedp_adapter .
go build -C runner/scripts/adapters/rod_adapter -o rod_adapter .
cargo build --manifest-path runner/scripts/adapters/chromiumoxide_adapter/Cargo.toml
```

所有 driver 的版本都固定在 `harness_pins.json` 里，`doctor` 会把实际安装的版本和 pin 逐一对照。

## 3. 跑前自检

正式开跑前，先过三道检查：

```bash
python3 -m runner.run doctor      # 引擎能启动吗？身份校验、pin、adapter 都过吗？
python3 -m runner.run validate    # 任务集完整性：1,928 道、18 个 subset
python3 -m pytest test -q         # 框架单元测试，不需要引擎二进制
```

三道门各管一件事，红了代表不同的意思：

- `doctor` 红 = **环境问题**：缺二进制、pin 对不上、adapter 没编译。
- `validate` 红 = **任务集对不上**：磁盘上的任务集和 manifest 不一致。
- 单测挂 = **框架自身坏了**。

这三种情况都不是浏览器能力的问题，**别把它们当成被测引擎的成绩**。

## 4. 开始运行

先跑冒烟测试（smoke，几分钟）确认全链路是通的：

```bash
python3 -m runner.run run --subset l1.raw_cdp --tag purpose.smoke \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent --chrome-baseline best_effort --seed smoke
```

再跑完整正式 run（这就是已发布结果所用的配置）：

```bash
python3 -m runner.run run \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent --chrome-baseline best_effort \
  --seed official20260709 --k 3 --jobs 16 --host-telemetry on \
  --run-id <explicit-name> --provenance-level minimal
```

参数速览：`--k 3` = 每道题尝试三次；`--jobs 16` = 16 路并行跑；`--host-telemetry on` = 开启宿主机资源遥测；`--provenance-level minimal` = 溯源级别只保留硬件事实、去掉部署信息。

## 5. 跑完会得到什么

一次 run 的全部产物都在 `runs/<run-id>/` 下：

| 文件 | 内容 |
|:---|:---|
| `run_manifest.json` | 完整溯源：引擎身份、harness pin、runner 源码树 / fixture 树 / 编译 adapter 的摘要 |
| `results.jsonl` | 每道题 × 引擎 × 尝试一行，带状态与失败归因 |
| `scores.json` | 按评测维度聚合后的分数 |
| `scorecard.md` | 给人看的摘要 |

## 6. 计分模式

- **`--score-mode independent`**：每个选中引擎只按自己的尝试计分。这是发布配置；Chrome 以参照列身份参与、不设门槛。默认的 `baseline_checked` 则启用 Chrome 基线策略，只给候选引擎计分。
- **`--chrome-baseline` 为什么用 `best_effort` 而不用 `required`**：如果选 `required`，Chrome 挂掉的题会被从所有引擎的计分分母里剔除，等于把 Chrome 自己的成功率"保送"到接近 100%——它作为参照列就失去意义了。

## 7. 资源测量

功能成绩和资源测量**不共用同一次 run**（功能轮归功能轮，资源轮归资源轮）。A/B 协议 = 同一台机器、同一套题、同一个 seed 跑两轮：

```bash
# A 轮：基线，profiler 关
python3 -m runner.run run ... --resource-profile baseline --jobs 1 --k 5 --score-mode independent

# B 轮：引擎轮，profiler 开，指向 A 轮做校准
python3 -m runner.run run ... --resource-profile engine --jobs 1 --k 5 --score-mode independent \
  --resource-calibration-baseline runs/<baseline-run>
```

B 轮结束时，拿自己的任务耗时分布和 A 轮对比，量出 **profiler（性能剖析器）本身对引擎的干扰**。CPU、内存（PSS，进程实际占用内存的估算）、进程数、页面流量——这些只有在干扰过了校准门（`resource_comparison_eligible: true`）时才会报告。完整契约见 [resource-cost.zh.md](resource-cost.zh.md)。

## 常见失败

- **`doctor` 报 pin 不匹配**：`build_artifacts/` 下的二进制不是你 pin 的那个构建。激活正确的 set，或者有意识地更新 `active-set.json`。
- **结果行大量 `infra`**：身份门没过——客户端没连到它该连的引擎。这是环境或路由问题，**永远不是**兼容性分数。
- **编译型 adapter 缺失**：用上面的 Go/Rust 命令重新编译；`doctor` 会打印它期待的确切命令。
