# REG-001 复测：模型判断 + 确定性控制层

## 结论

`v0.6.0` 候选实现完整保留 Target、Observe、Estimate、Decide、Act、Action Check、Target Check
七次模型阶段判断，同时把路径、截图保存、证据登记、字段别名、机械 Action 清理和硬断言交给
确定性控制层。

最终 TD 在 **110.193 秒**后进入 `succeeded`，0 次人工中断、1 个 Action、0 次结构化修复。
相比 2026-08-09 的 125.615 秒基线，墙钟时间减少 **15.422 秒（12.3%）**。本轮没有通过
跳过阶段来换取速度；主要收益是消除错误路径、重复截图归档、无意义人工确认和结构化修复。

## 测试标识

| 字段 | 值 |
| --- | --- |
| 日期 | 2026-08-10（Asia/Shanghai） |
| 标准场景 | `REG-001` |
| Run | `run_a27a4bf9` |
| Session | `sess-145d540e-20260810_003646` |
| User Thread | `ut_run_a27a4bf9` |
| TD | `td_b3cd4ab1` |
| 模型 | `deepseek-v4-flash` |
| 候选版本 | `0.6.0` |

## 结果

| 指标 | 旧基线 | v0.6.0 复测 | 变化 |
| --- | ---: | ---: | ---: |
| 墙钟时间 | 125.615s | 110.193s | -12.3% |
| 模型阶段操作 | 8 | 7 | -1 |
| 输入 Token | 18,543 | 21,855 | +17.9% |
| 输出 Token | 14,756 | 11,181 | -24.2% |
| 总 Token | 33,299 | 33,036 | -0.8% |
| 结构化修复 | 1 | 0 | -1 |
| 人工中断 | 0 | 0 | 不变 |
| Action | 1 | 1 | 不变 |
| 最终 TD 状态 | succeeded | succeeded | 不变 |

### 阶段耗时

| 阶段 | 耗时 |
| --- | ---: |
| Target | 23.379s |
| Observe（含真实浏览与截图） | 16.575s |
| Estimate | 14.615s |
| Decide | 20.462s |
| Act | 9.990s |
| Action Check | 9.373s |
| Target Check | 15.754s |

### 产物与证据

- Artifact：`act-report-generate.md`；
- 截图：有效 PNG，16,102 bytes，位于 Session `trace/.../screenshots/`；
- 证据登记包含 `path`、`sha256`、`size_bytes`、`source_url`、`page_title`、`body_text`；
- Action Check：确定性硬断言 + 模型语义断言全部通过；
- Target Check：确定性验收 + 模型负向约束审查全部通过。

## 外层 Oracle 修正

本次运行时 TD 已为 `succeeded`，但当时的 E2E 外层 Run 一度显示 `failed`。原因是 Oracle 仍只匹配
example.com 的旧文案 `illustrative examples`，而当前工具观察到的正文为 `documentation examples`。
该 Oracle 已更新为兼容当前和历史稳定语义。这是测试判定缺陷，不改变本次 TD 的成功状态和落盘轨迹。

## 中间失败样本

优化过程保留了三类失败轨迹，均转化为测试和控制规则：

1. `run_00d1c9dd`：工具事实存在，但证据元数据未登记 `page_title/body_text`，误请求人工；
2. `run_aa59932e`：远端分块响应 `IncompleteRead` 直接穿透 CLI；现已归一化进入恢复预算；
3. `run_e6ae42ca`：`title`/`page_title` 字段别名导致硬验收假失败；现已规范化。

失败样本不删除，用于异常经验与后续回归。
