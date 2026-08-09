# 持续交互会话设计

运行日志、目标持久化目录和 Credentials 隔离见
[CLI 准 Web 服务与持久化设计](runtime-storage-design.md)。现有目录保持兼容，迁移不得在启动时静默进行。

## 对象关系

```text
UserThread
├── messages.jsonl              跨 Session 的自然语言时间线
├── thread.json                 根 TD 与 session_ids
├── sessions/*.json            围绕同一需求显式开启的持久化会话
└── td/                         需求内部的父子 TD
    └── <td_id>/
        ├── state.json
        └── event/operation log
```

- 一个明确的用户需求对应一个 User Thread；
- Session 是依附于 User Thread 的持久化 `toe-dac-loop`，可以被多个 CLI Connection 反复接入；
- CLI 退出或进程重启只会 detach Connection，不创建或结束 Session；
- 只有用户显式执行 `session new` 时，才在同一 User Thread 下创建另一个 Session；
- TD 是 Thread 内部的规划与执行结构，不能用新的 TD 承载另一个用户需求；
- 每条消息关联 `td_id` 和 `session_id`；
- 根 TD 进入终态后保持不可变；新需求必须创建新 Thread；
- Thread 聚合其全部 Session 的消息、事件和产物。

## CLI

```bash
toe-dac continue --session ss_demo --model glm-5
toe-dac session new --thread ut_demo --model glm-5
```

自然语言输入由 Conversation Controller 根据当前 TD 状态解释。控制命令使用 `/` 前缀：

- `/status`：当前 Thread、TD、Session 和状态；
- `/why`：解释最近阻塞、异常或 Executor 边界；
- `/show target|observe|estimate|plan|action|errors|timing`：读取持久化控制面，不推进状态；
- `/continue`：继续当前 Agent Loop；
- `/reobserve [原因]`：保留轨迹并受控回到 Observe；
- `/replan [调整要求]`：保留旧 Plan 并受控回到 Decide；
- `/history`：最近消息；
- `/evidence`：只读打开当前 Session 的 `trace/sessions/<session>/` 证据目录；不创建、复制、聚合或刷新证据；
- `/pause`、`/resume`、`/cancel`；
- `/quit`：断开当前 CLI Connection，不结束 Session 或 TD。

在进入状态机前，Session 输入先经过本地对话路由：

- `greeting`：问候，只返回当前 Thread 摘要；
- `clarify`：解释当前阶段、阻塞原因和用户可执行选项；
- `status`：返回 Target、状态和待确认事项；
- `task_input`：需求、补充材料或对待确认问题的实质回答。

所有本地查询类意图都不调用模型、不修改 TD 状态，也不能在 `waiting_human` 时被记作 `human_answer`。
只有 `task_input` 可以恢复状态机。阶段内部的诊断问题不能直接展示给用户；`human_question`
必须说明用户能提供什么、能决定什么，或者有哪些暂停和终止选项。

## 自动推进边界

收到一条用户消息后，Controller 在预算内自动推进：

```text
Target → Observe → Estimate → Decide → Act → Action Check → Target Check
```

直到：

1. TD 到达 `succeeded` 或 `failed` 终态；
2. 阶段确实缺少只能由人提供的业务信息，进入 `waiting_human`；
3. Action 需要新增授权，进入人工授权边界。

`recovering` 是 Agent Loop 内部的瞬时状态，不是默认停靠点。模型格式错误、工具失败、
本地校验失败和 Action/Target 断言失败都必须在预算内自动重试或更换路径；预算耗尽后自动进入
`failed`，同时保存失败 Artifact 和完整尝试轨迹。一次用户输入应当尽可能“一次到底”，不能把
Agent 自己能够处理的内部故障转交给用户。

模型只提交当前阶段结构，Controller 校验并驱动状态机；模型不能直接写状态。

`agent_response` 是当前内置的安全 Executor：它只生成面向用户的文本，不执行外部变更。
候选回复先保存到 Thread artifacts，再检查 Action assertions，最后检查 Target acceptance criteria；
两级检查都通过后才向用户展示并将 TD 置为 `succeeded`。未显式标注 executor 的旧 Action
只有在目标和指令明显属于“向用户回复/输出/汇报”时才按 `agent_response` 执行，否则保持外部边界。

技能采用渐进加载。初始上下文只包含 persona 与 `skills/index.md`；技能正文仅在相关时加载。
技能工具受 phase scope 和单阶段调用预算约束，例如 `alex-serp` 只在 Observe 开放，最多调用 3 次。

CLI 通过瞬时 progress 事件显示模型调用、技能加载、工具调用、重试、结果数量和耗时。
`phase_run` 与 `generate_structured` operation 分别记录阶段总耗时和模型/工具生成耗时；访问日志仍负责请求时间线。

Observe、Estimate、Decide 的结构化结果还要通过本地语义校验。失败时保存无效 payload 和错误证据，
携带 `repair_feedback` 自动重试，预算耗尽后进入 `failed`，不请求用户修复模型输出。Estimate verdict 支持
`feasible`、`needs_observation` 与 `not_feasible`；验收必需信息缺失时通过受预算约束的转换回到 Observe，
明确 `not_feasible` 时以失败终态结束并生成评估 Artifact。
Decide 不得把搜索、访问、读取或抓取等事实收集工作放入 Action，这类计划会被拒绝并修复。
