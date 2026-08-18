# 结果怎么读

[English](RESULTS.md) · [中文](RESULTS.zh.md)

报告负责给出数字，这一页负责定义**数字的含义、边界，以及它们支撑不了什么结论**。

## 关键数字是什么

头条通过率计一道题通过的标准是：**全部 k 次尝试都通过**（已发布 run 的 k=3）。过了两次、超时一次，这道题按失败计。让头条数字对不稳定敏感是有意的：Agent 重试一个抖动的操作，消耗预算的方式跟撞上硬失败没有区别。

每个引擎只按自己的尝试计分（`--score-mode independent`）。Chrome 是参照列，不是及格线。另一种模式 `--chrome-baseline required` 会把 Chrome 失败的题从所有引擎的分母里剔除，等于把 Chrome 那列**保送**到接近 100%——这正是发布 run 采用 `best_effort` 的原因：Chrome 的 99.90% 是测出来的值，它挂掉的那两道题仍然留在所有人的分母里。

## 状态分类

每条结果行恰好带一个状态：

| 状态 | 含义 | 计入引擎成绩？ |
|:---|:---|:---|
| `pass` | 操作完成且所有检查通过 | 计为通过 |
| `fail` | 连上了也跑了，但操作或检查失败 | 计 |
| `unsupported` | 引擎报告该能力未实现 | 计，而且这是诚实的失败方式 |
| `timeout` | 任务预算耗尽 | 计 |
| `crash` | 引擎进程死亡 | 计 |
| `infra` | 身份门或环境失败，引擎没有被有效连接 | 不计；该行划归框架侧 |

失败的行还带 `failure.class` 和 `failure.origin`，报告因此能把一次失误归因到**协议面、页面语义、driver 栈或框架自身**，而不是只给一个裸计数。

## L1 轴：同一个行为，13 个生态

L1 刻意让同一个行为跨 driver 重复——因为 **driver 兼容性正是被测量的维度**。1,740 道 L1 里的 1,233 道，由 116 个**与 driver 无关**的场景定义展开而来。所以当一个引擎在 Puppeteer 下通过、在 Selenium 下失败时，这个差值本身就是发现。

**整列为零要读成"启动级失败"，而不是 92 次独立失误**：说明这个栈对该引擎连会话握手都完成不了，握手之后的题全都够不着。报告会把每个零列归因到根因。

已发布 run 还显示信号方向是**混合的**——这本身就是关于测试集的证据：Moli 总分领先，但 Lightpanda 在六个 subset 上高于 Moli（`l1.puppeteer` 114:96、`l1.agent_browser_scenarios` 80:66、`l1.cdp_use`/`l1.chrome_remote_interface`/`l1.stagehand` 76:69、`l1.agent_browser_tool` 68:64）。一套专门为某个引擎量身挑选的任务集，不会产生这种模式。

## L2 轴：按能力项计，不按题数计

L2 按行为判语义。fixture 跑在框架自己的服务器上，由**服务端判题器**检查可观察结果（DOM 状态、存储状态、网络服务端状态或工作流结果），而不是信协议的回声（CDP 报成功但页面没变化）。

原始任务行不是分母。188 道 L2 题经 [`config/l2_semantic_capabilities.json`](../config/l2_semantic_capabilities.json) 映射到 **72 个能力项**，每道题承担三种角色之一：

- `semantic_probe`：计入所属能力项的判定。一个能力项通过的条件 = 分配给它的**所有**计分 probe 全部通过；
- `driver_cross_check`：单独报告，不增加分母单元；
- `diagnostic`：提供失败定位证据，不增加分母单元。

这个"全对才算过"（合取）判定很严格，但**权重是一**：六个向量的 WebCrypto digest 家族能定位到具体算法的 bug，却不会把一个实现缺口放大成六次头条失败；反过来，能力项也不会靠"运气好的那一部分 probe"蒙混过关。在已发布 run 里，这套映射产出报告 L2 轴上的 **192 个计分单元**。

两道护栏保证稳定：

- **部分选题不算分**：某次 run 如果只选中一个能力项的部分 probe，该能力项直接标记为 `missing`，而不是拿一半证据悄悄算一个分。
- **配置可追溯**：run manifest 会为能力映射表做快照（路径 + sha256）；映射表演进之后，旧 run 依然可解释。

多步工作流任务的判定是**双门**的：driver 声明的步骤检查要全部完成，fixture 服务器还要按预期答案注册表验收提取出的最终答案。driver 侧的相等断言**从不单独决定**一个工作流的成绩。

## 资源数字怎么读

资源数字来自单独的 A/B 校准轮：契约见 [resource-cost.zh.md](resource-cost.zh.md)、复现步骤见 [REPRODUCE.zh.md](REPRODUCE.zh.md)。阅读时注意：

- 统计在四个引擎共同通过的 **1,045 个任务×尝试交集**上进行；失败的题从来不按"便宜的失败"计入任何引擎的账。
- 数字描述的是 **557 道**的资源任务集，不外推到全量。
- 实心值是中位数；资源卡片里还有 p95、PSS 增量、进程数和 fixture 流量。
- 远程引擎在测量机上**没有进程树**。空单元格的意思是"无法测量"，永远不是零。

## 五引擎对比

main 分支报告四个本地固定二进制。[`kitesurf-eval`](https://github.com/lexmount/Lexbench-Headless-Browser/blob/kitesurf-eval/README.zh.md) 分支加入远程端点 Kitesurf，并在**显式定义的可比任务子集**上发布对比：先剔除"跨远程边界后失败无法归因"的 subset，再进一步剔除端到端系统性阻塞的 subset。子集定义、裁定规则和五引擎表都在那个分支的报告里；一句话版本是：**远程端点只有在显式声明的分母之内才可比。**

## 为什么测这些机制

引擎团队自己发布的 benchmark，第一个被问的问题一定是：**题是不是挑着对自家有利的？**

任务集的覆盖面来自对 pinned 版 Playwright、Puppeteer、agent-browser 等栈**实际调用面**的调研，再按真实框架会打到的路径补题；每道题必须在 Chrome 上通过才有入库资格。上面那组混合的 subset 信号，就是可观察的结果。

L2 选的机制是生产页面真实依赖的，并配了第三方使用数据：

- **选择器是日常承重面。** Chrome use counter 显示：截至 2026-06，51.7% 的页面加载命中使用了 `:has()` 的页面，一年前是 40.6%（[chromestatus，bucket 4743](https://chromestatus.com/metrics/css/timeline/popularity/4743)）。Project Wallace 2026 对头部站点首页 CSS 的爬取显示：41.3% 的样式表用了 `:has()`、76.8% 用了 `:nth-child`、53.2% 用了 `:nth-of-type`（[The CSS Selection 2026](https://www.projectwallace.com/the-css-selection/2026)）。**选择器算错 = 内容或可见性状态算错，不是外观瑕疵。**
- **客户端存储是沉默的基础设施。** Chrome 最后一批公开计数显示：19.3% 的页面加载执行了 IndexedDB 读、16.6% 执行了写（[bucket 3023](https://chromestatus.com/metrics/feature/timeline/popularity/3023)、[bucket 3024](https://chromestatus.com/metrics/feature/timeline/popularity/3024)）；Firestore 的 web 离线持久化底层就是 IndexedDB（[文档](https://firebase.google.com/docs/firestore/manage-data/enable-offline)）。促成这批题的正是 Agent 场景特有的失败方式：引擎对缺失的存储 API **优雅降级**，页面照常渲染但数据为空，Agent 会自信地给出错误答案而不是报错——只有存储语义题能暴露这一点。
- **driver 的交互原语直接消费 layout 和 computed style。** Playwright 的 actionability 规则要求每次 click/fill 前有非空 bounding box、computed 可见性和 hit-target 检查（[playwright.dev/docs/actionability](https://playwright.dev/docs/actionability)）。样式或布局不完整的引擎，丢掉的不是某一道题，而是**框架自动等待与点击的底座**。Lightpanda 自己的文档写明其 Web API 覆盖不完整（[lightpanda.io/docs](https://lightpanda.io/docs/)），与它 L2 失误的聚集位置一致。

## 边界

- **截图、PDF 与光栅输出不在当前测量范围内。** 这是**暂缓**而不是永久结论：这条边界划定于候选引擎普遍还没有 paint 管线的时期，已列入重新评估。这里的一切都不测像素正确性。
- 功能结果出自一组 pinned 引擎在**一类机器**上的表现。不同构建就是不同软件——比较数字之前，先比较 manifest。
- Chrome 的 99.90% 是测出来的，**不是公理**。它失败的那两道题在报告里可见，且留在所有分母中。
