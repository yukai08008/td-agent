# TOE-DAC 标准回归测试

## 1. 目的

标准回归测试验证的不是“模型偶尔能做对”，而是 TOE-DAC 在相同输入和授权边界下能够：

1. 按阶段协议推进并保持状态一致；
2. 在预算内自动恢复内部失败，一次运行到成功或明确失败；
3. 只在缺少人类信息或新增授权时中断；
4. 保存可复核的日志、证据、Artifact 和异常经验；
5. 在进程重启和 Session 重新接入后保持同一 User Thread 语义。

每个线上缺陷都应补充一个稳定的回归编号。标准用例不得只断言 CLI 文案，必须检查状态、结构化数据和落盘证据。

## 2. 测试分层

| 层级 | 运行时机 | 外部依赖 | 验证内容 | 通过要求 |
| --- | --- | --- | --- | --- |
| Unit/Contract | 每次提交 | 无 | Schema、状态转换、路由、预算 | 全部通过 |
| Deterministic E2E | 每次提交 | 无 | 完整 TD 生命周期和持久化 Oracle | 全部通过 |
| Live E2E | 发布前 | 模型、网络、技能工具 | 真实模型和 Executor 稳定性 | 连续 3 次通过 |
| Recovery E2E | 发布前 | 可控故障注入 | 自动重试、换路径、失败终态 | 无非预期人工中断 |

## 3. 全局不变量

所有标准场景共同检查：

- 一个明确需求只创建一个 User Thread；同一需求可以包含多次 Session；
- 一次有效输入触发 Agent Loop，直到 `succeeded`、`failed` 或合理的 `waiting_human`；
- `recovering` 不能成为默认停靠点；
- Model/Tool/Validation 内部错误不得直接转交给用户；
- `action_check` 和 `target_check` 分别执行，不能以 Action 成功代替目标成功；
- 成功和失败都产生 Artifact；异常的出现、处理和结果写入经验数据；
- `event.jsonl`、`opr.jsonl` 和 Session 证据为追加式记录；只读命令不得刷新原文件时间；
- 截图证据必须是工具真实产生的有效图片，不能由模型声称存在。
- 证据实体始终位于 Session 的 `trace` 下；`/evidence` 只是只读访问入口，不是目录名或归档动作。

## 4. 标准场景目录

| 编号 | 类型 | 核心目的 | 当前自动化位置 |
| --- | --- | --- | --- |
| `REG-001` | Deterministic + Live | 公开网页取证、中文报告、截图、一次到底 | `toe-dac run REG-001` |
| `REG-002` | Recovery | 模型首次结构化失败后自动恢复并完成 | `tests/test_conversation.py` |
| `REG-003` | Contract | Estimate 缺少 `cost` 等字段时自动修复 | `tests/test_conversation.py` |
| `REG-004` | Persistence | Thread/Session 重新接入后继续原 TD | `tests/test_conversation.py`、`tests/test_cli_sessions.py` |
| `REG-005` | Evidence | `/evidence`、只读查看不改写原证据 | `tests/test_storage_migration.py`、`tests/test_cli_sessions.py` |
| `REG-006` | Experience | 异常—处理—成功/失败经验累计与采用 | `tests/test_persistence_experience.py` |
| `REG-007` | Human boundary | 仅实质歧义或授权请求人类 | `tests/test_targeting.py`、`tests/test_conversation.py` |
| `REG-008` | Failure terminal | 恢复预算耗尽后明确失败并产生 Artifact | `tests/test_conversation.py`、`tests/test_flow.py` |

## 5. REG-001：Example.com 网页取证与中文报告

### 5.1 用户原始输入

> 访问 https://example.com，确认页面标题和主要内容，生成一份简短中文报告，并保留网页截图作为证据。

这段文字是测试输入的一部分，不允许在运行时偷偷补充标题或正文事实。

### 5.2 权限和前置条件

- 允许访问 `https://example.com`；
- 允许只读浏览和保存本地截图；
- 不允许修改远端内容或访问无关网站；
- Live 模式需要可用模型配置，以及 PATH 中的 `agent-browser` 和 Node.js；
- 最大 12 次模型调用、2 个 Action、3 次恢复、180 秒墙钟时间。

### 5.3 预期阶段路径

```text
Target（只定义页面事实与中文报告效果，不将截图写入 Target）
→ Observe（加载 agent-browser，访问页面，提取标题和正文，运行时自动保存截图）
→ Estimate（判断证据足够且可行）
→ Decide（只规划中文报告，不把网页访问错误放进 Action）
→ Act（生成报告 Artifact）
→ Action Check（报告和截图引用存在）
→ Target Check（标题、主要内容、中文报告、截图全部满足）
→ succeeded
```

允许内部出现 `automatic_retry`，但预算内恢复后必须继续运行，不能停在 `recovering`。

### 5.4 Oracle

以下条件必须同时为真：

| 检查项 | 判定方式 |
| --- | --- |
| 终态 | TD state 为 `succeeded` |
| URL | Observation 引用 `https://example.com` |
| 标题 | Observation 包含 `Example Domain` |
| 主要内容 | Observation 说明该域名用于文档/示例用途 |
| 报告 | Artifact 存在，包含中文字符、标题和主要内容 |
| 截图 | 作为 Observe 运行时不变量，Session `screenshots/` 下至少一个 `observe-*.png`；PNG 签名正确且大小大于 8 字节 |
| 双层检查 | Action Check 与 Target Check 均通过 |
| 人工介入 | `human_interrupts == 0` |
| 留痕 | operation 中存在浏览器调用；event、operation、messages、evidence 均可回溯 |

模型措辞不要求逐字一致；Oracle 只检查事实和产物，不以某段固定回复作为成功标准。

### 5.5 失败分类

| 失败 | 期望处理 |
| --- | --- |
| 模型返回非法 JSON/Tool arguments | 自动修复或自动重试；预算耗尽后 `failed` |
| `agent-browser` 暂时失败 | 自动重试或换可验证路径，不立即问人 |
| 页面可访问但截图失败 | Target 不得通过；继续恢复，耗尽后 `failed` |
| 只有标题、没有主要内容 | Estimate 返回信息缺口并重新 Observe |
| 报告生成成功但截图不存在 | Action Check 或 Target Check 失败，不能 `succeeded` |
| 缺少浏览器程序 | 环境能力失败；记录明确原因并以失败终态结束 |

### 5.6 执行方式

每次提交运行确定性版本：

```bash
uv run pytest tests/test_e2e_runner.py -q
uv run toe-dac run REG-001 --mode mock
```

发布前运行真实版本：

```bash
toe-dac doctor
toe-dac run REG-001 --mode live \
  --model deepseek-v4-flash \
  --model-config ~/.config/td-agent/models.json
```

查看结果：

```bash
toe-dac report <run_id>
```

Live 发布门槛为同一候选版本连续运行 3 次全部通过。失败 Run 不删除，作为问题复现和经验数据来源。

性能历史与复测方法保存在 [benchmarks/](benchmarks/README.md)。`REG-001` 的首个开发基线为
[2026-08-09 deepseek-v4-flash 对比报告](benchmarks/2026-08-09-reg-001-deepseek-v4-flash.md)。

## 6. 新增标准场景的要求

新增 `REG-*` 用例至少需要：

1. 稳定的用户原始输入；
2. 明确的允许/禁止边界；
3. 正常路径和故障路径；
4. 与模型措辞无关的确定性 Oracle；
5. 成功、失败、人工中断的精确预期；
6. 日志、证据和 Artifact 检查；
7. 一个可在 CI 中运行的确定性测试；
8. 如涉及真实工具，再提供发布前 Live 运行方式。
