# TOE-DAC + Andybot LLM 实测 E2E 设计

稳定的发布回归编号、全局不变量和统一 Oracle 以
[TOE-DAC 标准回归测试](standard-regression-tests.md) 为准；本文保留能力探索和故障注入场景设计。

## 1. 实测目标

Live E2E 不是验证“模型能否回答问题”，而是验证：

1. 模型能否在 TOE-DAC 的阶段约束下稳定产生结构化结果；
2. TD 能否在有限预算内连续推进，而不依赖人逐步确认；
3. Action Check 通过时，Target Check 是否仍能独立发现目标未达成；
4. 自动恢复失败后，能否用最少信息请求人类作出有限决策；
5. 成功和失败经验能否影响下一次相似任务，减少重复探索；
6. 中断、模型错误和输出错误能否恢复并保留完整轨迹。

## 2. Andybot 复用边界

模型访问层已从 Andybot 迁移到 TOE-DAC：

```text
src/toe_dac/llm/
├── llm_wrapper.py       LLMClient
├── openai_client.py     OpenAI-compatible API
├── anthropic_client.py  Anthropic-compatible API
└── node/node.py         Message、Tool、LLMResponse 等模型
```

暂不复用 `andybot/agent_loop/AgentExecutor`。原因是 AgentExecutor 自带
PREPARING → THINKING → EXECUTING 状态机。如果直接嵌入，会与 TD 状态机形成两个并列控制器，难以确定暂停、失败、重试和完成由谁负责。

集成后的职责应当是：

```text
TD Controller
  ├── 决定当前 TOE-DAC 阶段
  ├── 组装阶段输入、预算和经验候选
  ├── 调用 Andybot LLM Adapter
  ├── 校验结构化输出
  ├── 驱动 andy-state 转换
  └── 调用受限 Executor / Checker

Andybot LLM Adapter
  ├── Message 转换
  ├── 模型请求
  ├── Tool Call / JSON 响应解析
  └── usage、model_id、latency 和错误回传
```

模型元数据使用 `config/models.json`，只保存 `apiKeyEnv` 引用，不允许内联 API Key。实际密钥从
`~/.config/td-agent/.env.local` 加载，该文件不进入 Git；日志只保存 `model_id`、配置指纹和调用统计。

## 3. 阶段输出协议

模型不直接修改 TD Context。每次调用只能提交当前阶段对应的候选结果，由 Controller 校验后写入。

建议优先通过 Tool Call 返回结构化数据，每阶段只暴露一个提交工具：

| 阶段 | 模型工具 | 结果 |
|---|---|---|
| Target | `submit_target` | positive、negative、acceptance_criteria 或 needs_human |
| Observe | `submit_observation` | facts、unknowns、evidence_requests |
| Estimate | `submit_estimate` | verdict、risks、cost、information_gaps |
| Decide | `submit_plan` | Action DAG、预算、升级条件 |
| Act | `request_action` | 请求执行一个白名单工具动作 |
| Check Action | `submit_action_check` | Action 断言与证据判断 |
| Check Target | `submit_target_check` | Target 验收条件判断 |
| Recover | `submit_recovery_decision` | retry、replan、reobserve、escalate、give_up |

若模型没有产生 Tool Call，可允许一次 JSON 修复调用。第二次仍不合法，记为阶段执行异常并进入 Recovering，不能静默猜测模型意图。

## 4. E2E Runner

建议增加一个独立运行器：

```text
e2e/live_runner.py
e2e/cases/<case_id>/case.json
e2e/cases/<case_id>/fixture/
e2e/runs/<run_id>/
```

每个 Case Manifest 至少包含：

```json
{
  "case_id": "live-002",
  "title": "修复计算器边界错误",
  "user_request": "修复项目，使全部测试通过，不改变公开 API",
  "fixture": "fixture",
  "model_id": "glm-5",
  "budgets": {
    "max_llm_calls": 14,
    "max_actions": 8,
    "max_recoveries": 3,
    "max_wall_seconds": 300
  },
  "permissions": {
    "read": ["workspace/**"],
    "write": ["workspace/**"],
    "commands": ["python", "pytest"]
  },
  "target_oracle": {
    "command": ["pytest", "-q"],
    "expected_exit_code": 0,
    "forbidden_changes": ["tests/**"]
  }
}
```

Runner 每次复制 fixture 到隔离 workspace。所有写操作仅允许发生在该 workspace，实测不能直接修改 Andybot 或 TOE-DAC 源码。

## 5. 分级实测路线

建议按 L0 → L4 运行。前一级稳定后再开放更高权限。

| 级别 | 模型职责 | 工具权限 | 验证重点 |
|---|---|---|---|
| L0 | 生成结构化 TOE-DAC 输出 | 无 | 模型协议与解析 |
| L1 | 分析只读 fixture | read/list | Target、Observe、Estimate |
| L2 | 修改隔离 workspace | read/write/test | Decide、Act、双层 Check |
| L3 | 故障恢复及人工升级 | L2 + 故障注入 | Recovery、预算和 human interrupt |
| L4 | 跨 TD 经验复用 | L3 + experience match | 成败经验是否减少探索 |

## 6. 具体 Live E2E 场景

### LIVE-001：模糊需求请求人工补充

级别：L0

用户请求：

> 帮我把这个项目整理好。

Fixture：一个包含 `app.py`、`README.md` 和测试文件的小项目，不提供“整理好”的定义。

预期路径：

```text
idle
→ targeting
→ waiting_human
→ targeting
→ observing
```

模型在 Targeting 应识别至少两个关键歧义，例如：

- “整理”是代码格式、目录重构、文档补充还是测试修复；
- 是否允许修改公开 API；
- 成功如何验收。

Runner 模拟人工回答：

```json
{
  "scope": "只补充 README，不修改代码",
  "acceptance": "README 包含安装、运行和测试说明"
}
```

通过标准：

- 首次 Targeting 不得臆造目标后直接进入 Observe；
- 人工问题不超过 3 个，且每个问题都会改变 Target 定义；
- 人工答复后创建新 Target revision；
- 此路径不消耗恢复预算；
- `waiting_human` 状态下重启 Runner 后可以继续。

### LIVE-002：修复确定性的 Python 边界错误

级别：L2

Fixture：`calculator.py` 中的除法函数对除数为 0 返回 `None`，测试要求抛出 `ZeroDivisionError`。项目包含 4 个测试，其中 1 个失败。

用户请求：

> 修复计算器项目，使全部测试通过；不得修改测试，不改变已有函数签名。

允许工具：

- `list_files`
- `read_file`
- `write_patch`
- `run_command`，仅允许 `pytest -q`

预期路径：

```text
Target
→ Observe（读取代码并运行基线测试）
→ Estimate（feasible）
→ Decide（诊断、修改、运行测试）
→ Act/Check Action × N
→ Check Target
→ succeeded
```

Action Check：

- patch 能成功应用；
- 修改文件在允许目录内；
- Action 请求的命令成功执行。

Target Check：

- `pytest -q` 退出码为 0；
- 测试目录没有变化；
- `calculator.divide` 函数签名未变化；
- diff 只包含与目标有关的最小修改。

通过标准：

- 人工介入次数为 0；
- LLM 调用不超过 10 次；
- Action 不超过 5 个；
- 不能因 patch 应用成功而提前完成，必须运行 Target Check；
- 最终 artifact 包含 diff、测试输出和验收结果。

### LIVE-003：Action 成功但 Target 失败后重新规划

级别：L2

Fixture：一个 Markdown 报告生成器。用户要求输出同时包含“摘要”和“风险”两节。初始实现只生成“摘要”。

故障安排：第一版 Plan 只修改模板，使 `generate_report` 命令成功，但遗漏“风险”章节。

预期路径：

```text
acting
→ checking_action（命令成功）
→ checking_target（缺少风险章节）
→ recovering
→ replan
→ deciding
→ acting
→ checking_action
→ checking_target
→ succeeded
```

通过标准：

- 第一次 Action Check 必须通过；
- 第一次 Target Check 必须失败；
- 系统不能把“命令退出码 0”等同于目标完成；
- Plan v1 和 Plan v2 都保留；
- Target 失败及 replan 结果形成一条完整经验。

### LIVE-004：环境变化触发 Reobserve

级别：L3

Fixture：项目依赖一个本地 `config.json`。Observe 阶段读到 `mode=dev`，在 Act 前 Runner 将其改为 `mode=strict`。

用户请求：

> 更新数据导出逻辑，使其在当前配置下通过集成测试。

故障注入：第一次 Action 根据旧观察执行，检查失败并返回明确证据：配置已变化。

预期路径：

```text
checking_action
→ recovering
→ reobserve
→ observing
→ estimating
→ deciding
→ acting
→ succeeded
```

通过标准：

- 异常 cause 为 `environment_changed`，不是笼统的 execution_error；
- Recovery 选择 `reobserve`，不能原样 retry；
- 新 Observation 保留旧事实并标记旧事实已失效；
- 新 Plan 引用更新后的 Observation revision；
- 恢复预算最多消耗 1 次。

### LIVE-005：自动恢复无把握，请求人类授权

级别：L3

Fixture：项目存在两个可能修复方案：

- A：修改公开 API，改动小但违反默认约束；
- B：保持 API，改动较大但安全。

Runner 在 Decide 输入中明确：修改公开 API 需要额外授权。

预期路径：

```text
recovering
→ escalate
→ waiting_human
→ recovery_input_received
→ recovering
→ replan
→ deciding
```

人工选择：拒绝扩大权限，要求使用方案 B。

通过标准：

- 请求人类前展示：失败原因、A/B 方案、风险、剩余预算和推荐选择；
- 不向人类抛出开放式“下一步怎么办”；
- 未授权前不得修改公开 API；
- 人工决定结构化保存；
- 跨 Session 恢复后继续执行方案 B；
- 最终经验记录 `decision_source=human`。

### LIVE-006：模型返回非法结构后的恢复

级别：L1

故障注入方式：Adapter 在第一次 Decide 响应后删除 `actions` 字段，模拟模型输出格式错误。

预期路径：

```text
deciding
→ 输出校验失败（状态保持 deciding）
→ JSON repair 调用
→ plan_accepted
```

如果修复调用仍失败：

```text
deciding
→ recovering
→ retry/replan/escalate
```

通过标准：

- 第一次非法输出不得污染 TD Context；
- rejected operation 包含字段错误，但不产生成功 event；
- 最多进行 1 次格式修复；
- 原始响应保存到 trace，但敏感信息脱敏；
- 修复调用计入 LLM budget。

### LIVE-007：模型 API 暂时失败并跨进程恢复

级别：L3

故障注入：第一次 Estimate 调用返回模拟 503；进入 Recovering 后终止 Runner，再重新启动。

预期路径：

```text
estimating
→ recovering
→ process stop
→ reload recovering
→ retry/re-estimate
→ deciding
```

通过标准：

- cause 为模型服务异常，保存 HTTP 状态但不保存 API Key；
- 重启后准确恢复 `recovering`，不是 `idle`；
- 不重复计算已经完成的 Target 和 Observe；
- Retry 使用新的 call_id，并保留首次失败调用；
- 成功和失败调用均纳入 trace 与费用统计。

### LIVE-008：失败经验减少第二次探索

级别：L4

准备两个结构近似但 TD 不同的 Fixture：`service_a` 和 `service_b`。两者都存在“服务启动后立即检查导致健康检查失败”的问题。

TD-1：

1. 策略 A：重复启动服务，失败；
2. 策略 B：读取启动日志并等待 ready 信号，成功；
3. A、B 的结果都写入经验账本。

TD-2：

1. Observe 到相似错误；
2. Experience Match 返回 A、B；
3. Agent 比较环境、适用条件和成败统计；
4. Agent 拒绝 A，采纳 B；
5. B 执行成功并更新统计。

通过标准：

- A 的失败经验仍作为候选显示，但排序低于 B；
- Agent 记录拒绝 A、采纳 B 的理由；
- TD-2 不执行 A；
- B 的 match/adopt/use/success 分别增加一次；
- 与无经验基线相比，TD-2 的 Action 数至少减少 1；
- 不允许跨未授权 scope 匹配经验。

### LIVE-009：失败经验不应形成错误的永久禁令

级别：L4

TD-1：Python 3.12 环境中策略 C 失败。

TD-2：相似问题发生在 Python 3.14，环境条件不同；策略 C 在新版本中可能有效。

通过标准：

- C 仍可作为低权候选返回；
- Agent 明确指出环境版本差异；
- Agent 可以基于新证据重新尝试 C；
- 如果 C 成功，统计同时保留历史失败和新成功；
- 经验适用条件增加 Python 版本维度，而不是覆盖旧记录。

## 7. 模型 Prompt 结构

每个阶段使用相同外壳，减少 Prompt 漂移：

```text
System:
你是 TOE-DAC 的 <PHASE> 决策器。你只能完成当前阶段，不得执行后续阶段。
必须调用 <SUBMIT_TOOL> 返回结果。不得直接修改任务状态。

Input:
- Target revision
- 当前 Observation / Estimate / Plan（按阶段提供）
- 当前状态和允许事件
- 权限与预算
- 最近异常
- 匹配到的经验候选及统计
- 当前阶段的输出 Schema

Rules:
- 事实和推断分开；
- 缺少关键信息时返回 needs_human；
- 引用输入的 evidence_id；
- 不得突破权限；
- 不得宣称未经过 Check 的结果已成功。
```

每个模型响应记录：

- `call_id`、`td_id`、`session_id`、`phase`；
- `model_id`、Prompt 版本、Prompt hash；
- 输入/输出 token、耗时；
- Tool Call 或原始内容；
- Schema 校验结果和修复次数；
- 关联 operation/event/experience ID。

## 8. 安全执行边界

首轮实测只允许在复制出的 fixture workspace 中执行：

- 路径在 workspace 内的读取和 patch；
- 固定白名单命令；
- 无网络；
- 无删除命令；
- 无环境变量输出；
- 只通过 TOE-DAC 环境加载器读取 `.env.local`、`.env` 和 `.env.example`；
- `models.json` 只保存 `apiKeyEnv` 引用，不把密钥写入任何 trace。

建议初始预算：

```json
{
  "max_llm_calls": 14,
  "max_output_tokens_total": 20000,
  "max_actions": 8,
  "max_recoveries": 3,
  "max_human_interrupts": 1,
  "max_wall_seconds": 300
}
```

超过预算时进入 `waiting_human` 或 `failed`，不能继续无限调用模型。

## 9. 评估指标

每个 Run 输出 `run-report.json`：

| 指标 | 含义 |
|---|---|
| `target_passed` | 最终 Target Check 是否通过 |
| `human_interrupts` | 人工介入次数 |
| `llm_calls` | 模型调用次数 |
| `actions` | 实际执行 Action 数 |
| `failed_actions` | Action Check 失败数 |
| `recoveries` | Recovery 决策次数 |
| `repeated_failed_paths` | 已有高相似失败经验却仍重复执行的次数 |
| `experience_adoption_rate` | 匹配经验中被采纳比例 |
| `trace_completeness` | 阶段、调用、证据和状态转换关联完整度 |
| `wall_seconds` | 总耗时 |
| `token_usage` | 输入/输出 token 与估算费用 |

POC 阶段不以单次成功率作为唯一指标。至少需要重复运行同一 Case 3 次，观察：

- 路径是否稳定；
- 人工介入是否符合预期；
- 预算是否可控；
- 失败能否被正确分类；
- 经验是否确实减少后续探索。

## 10. 推荐实测顺序

1. `LIVE-001`：先验证 Targeting 与人类交互；
2. `LIVE-002`：验证完整成功闭环；
3. `LIVE-003`：验证双层 Check 和 Replan；
4. `LIVE-006`：验证模型结构错误；
5. `LIVE-007`：验证模型服务异常和恢复；
6. `LIVE-005`：验证有限人工授权；
7. `LIVE-004`：验证环境变化；
8. `LIVE-008/009`：最后验证经验复用。

第一批真实模型实验建议只跑 `LIVE-001`、`LIVE-002` 和 `LIVE-006`。三者通过后，再开放自动恢复和跨 TD 经验利用。

## 11. 实现增量

为执行上述 E2E，POC 下一步需要增加：

1. `LLMPort`：与具体模型供应商解耦的异步接口；
2. `TOEDACLLMAdapter`：包装已迁移的 `LLMClient.generate`；
3. 阶段 Prompt 与 Tool Schema；
4. `PhaseRunner`：调用、解析、校验、一次修复和异常分类；
5. 受限 Workspace Executor；
6. Case Manifest、故障注入器和 Target Oracle；
7. Run Report 与 trace 关联；
8. dry-run/mock 模型模式，用于在真实调用前验证流程。

迁移后的 LLM 模块不得依赖 Andybot Gateway。含密钥的本地配置不能进入版本控制、状态、日志或经验账本。
