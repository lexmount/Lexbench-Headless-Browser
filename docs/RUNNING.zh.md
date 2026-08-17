# 把 bench 跑起来

[English](RUNNING.md) · [中文](RUNNING.zh.md)

本文档目标为让你在自己机器上完成一次完整 run。如果你的目标是精确复现已发布的数字，看完这页后接着读 [REPRODUCE.zh.md](REPRODUCE.zh.md)。

## 测哪些浏览器

| 引擎 | 角色 | 二进制从哪来 |
|:---|:---|:---|
| [Moli](https://github.com/lexmount/moli) | 候选 | 仓库 Releases，或按其构建文档自行编译 |
| [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) | 参照列 | 官方版本页下载（chromedriver 同页配套） |
| [Lightpanda](https://github.com/lightpanda-io/browser) | 候选 | 仓库 Releases，或按其构建文档自行编译 |
| [Obscura](https://github.com/h4ckf0r0day/obscura) | 候选 | 仓库 Releases，或按其构建文档自行编译 |

`--engines` 接受这四个名字的任意逗号组合，默认全选。chromedriver 不是被测对象，它只是 Selenium 路由需要的桥。远程引擎 Kitesurf 不走 `--engines`：它那条 lane 由配方机制驱动，见 [kitesurf-deployment.zh.md](kitesurf-deployment.zh.md)。

## 最短路径：先跑通一个引擎

只想最快看到一个引擎跑起来，不需要四个二进制、也不需要 Go/Rust/Ruby 工具链（它们只服务对应的编译型 adapter subset）。放好一个引擎的二进制，装 Node 依赖，然后在裸 CDP subset 上跑 smoke：

```bash
npm ci
python3 -m runner.run run --subset l1.raw_cdp --tag purpose.smoke \
  --engines chrome --score-mode independent --seed smoke
```

把 `chrome` 换成 `moli`、`lightpanda` 或 `obscura` 就是别的引擎。注意边界：单引擎 run 的结果行照常写入 `results.jsonl`（每道题的 pass/fail 都能看），但正式计分要求完整引擎名单——部分名单的 run 一律 `score_included: false`。它用来验证环境和观察单个引擎，不用来报数。

## 前提

Linux（暂未支持其他平台），且启用 cgroup v2（资源遥测要读 cgroup 和进程树）。需要 Python 3.11+ 和 Node 20。Go、Rust、Ruby 只有编译型 adapter 才用得到：`chromedp` 和 `rod`（Go）、`chromiumoxide`（Rust）、`ferrum`（Ruby）。

## 1. 引擎二进制

引擎二进制不入库，从上表「二进制从哪来」一列获取；复现已发布数字时，版本必须与报告溯源表里的 pin（版本 + sha256）一致。把一组固定版本的二进制放到 `build_artifacts/sets/<name>/` 下，附上 `set.json` 清单，然后激活：

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

`runner/run.py` 里的 `ENGINE_DEFS` 记录证据 pin（版本 + sha256）；`build_artifacts/active-set.json` 按机器覆盖。任何 run 之前 `doctor` 都会拿 pin 逐个校验二进制——放错或被改过的二进制会大声失败，而不是安静地产出数字。

## 2. driver 依赖

```bash
npm ci                              # Node 侧 driver，由 package-lock.json 固定
pip install -e '.[drivers,dev]'     # selenium + pydoll 的 pin，以及 pytest
gem install ferrum -v 0.17.2
go build -C runner/scripts/adapters/chromedp_adapter -o chromedp_adapter .
go build -C runner/scripts/adapters/rod_adapter -o rod_adapter .
cargo build --manifest-path runner/scripts/adapters/chromiumoxide_adapter/Cargo.toml
```

所有 driver 版本都固定在 `harness_pins.json` 里，`doctor` 会把实际安装的版本和 pin 逐一对照。

## 3. Inspection

```bash
python3 -m runner.run doctor      # 引擎能启动、身份校验、pin、adapter
python3 -m runner.run validate    # 任务集完整性：1,928 道、18 个 subset
python3 -m pytest test -q         # 框架单元测试，不需要引擎二进制
```

每道门红了各代表什么：`doctor` 红是环境问题（缺二进制、pin 不匹配、adapter 没编译）；`validate` 红说明磁盘上的任务集和 manifest 对不上；单测挂说明框架本身坏了。这三种情况都不该被解读成浏览器的结果。

## 4. Run

smoke（几分钟）：

```bash
python3 -m runner.run run --subset l1.raw_cdp --tag purpose.smoke \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent --chrome-baseline best_effort --seed smoke
```

完整正式 run（即已发布结果的配置）：

```bash
python3 -m runner.run run \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent --chrome-baseline best_effort \
  --seed official20260709 --k 3 --jobs 16 --host-telemetry on \
  --run-id <explicit-name> --provenance-level minimal
```

## 5. 一次 run 产出什么

全部落在 `runs/<run-id>/`：

| 文件 | 内容 |
|:---|:---|
| `run_manifest.json` | 完整溯源：引擎身份、harness pin、runner 源码树 / fixture 树 / 编译 adapter 的摘要 |
| `results.jsonl` | 每 task × 引擎 × attempt 一行，带状态与失败归因 |
| `scores.json` | 按评测轴聚合的分数 |
| `scorecard.md` | 给人看的摘要 |

## 6. 计分模式

`--score-mode independent`：每个选中的引擎只按自己的 attempt 计分。这是发布配置；Chrome 以参照列身份参与，不设门。默认的 `baseline_checked` 则启用 Chrome 基线策略、只给候选引擎计分。

`--chrome-baseline` 用 `best_effort` 而不是 `required` 的原因：`required` 会把 Chrome 失败的题从所有引擎的分母中剔除，Chrome 自己那列会被构造性地推向 100%，作为对照列就失去了意义。

## 7. 资源测量

功能分和资源测量不共用一次 run。A/B 协议是同机器、同任务集、同 seed 的两次 run：

```bash
# A 轮：基线，profiler 关
python3 -m runner.run run ... --resource-profile baseline --jobs 1 --k 5 --score-mode independent

# B 轮：引擎轮，profiler 开，指向 A 轮做校准
python3 -m runner.run run ... --resource-profile engine --jobs 1 --k 5 --score-mode independent \
  --resource-calibration-baseline runs/<baseline-run>
```

B 轮结束时拿自己的任务时长分布和 A 轮对比，量出 profiler 本身对引擎的干扰。CPU、PSS、进程数、fixture 流量只有在干扰过了校准门（`resource_comparison_eligible: true`）时才报告。完整契约见 [resource-cost.zh.md](resource-cost.zh.md)。

## 常见失败

- `doctor` 报 pin 不匹配：`build_artifacts/` 下的二进制不是 pin 的那个构建。激活正确的 set，或有意识地更新 `active-set.json`。
- 结果行大量 `infra`：身份门失败，客户端没有连到它该连的引擎。这是环境或路由问题，永远不是兼容性分数。
- 编译型 adapter 缺失：用上面的 Go/Rust 命令重新编译；`doctor` 会打印它期望的确切命令。
