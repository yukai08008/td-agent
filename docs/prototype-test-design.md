# TOE-DAC 交互式原型测试设计

## 1. 测试目标

验证交互式原型满足以下核心契约：

- 只有合法事件可以改变状态；
- Guard 拒绝时不破坏状态和业务上下文；
- Targeting 的输入错误、信息不足和执行异常走不同路径；
- Action Check 与 Target Check 不混淆；
- 恢复过程受预算限制；
- 暂停、人工等待和进程重启后可以准确恢复；
- 状态快照、事件日志和操作日志相互一致；
- 只有 Target Check 通过才能进入 `succeeded`。

## 2. 测试层次

| 层次 | 范围 | 主要验证内容 |
|---|---|---|
| 单元测试 | Guard、数据校验、Action 选择器 | 输入边界和纯函数结果 |
| 状态机测试 | Graph + Machine | 状态、事件、Guard 和上下文变化 |
| 持久化测试 | StateStore、JSONL 日志 | 保存、恢复、原子性和可追溯性 |
| 经验测试 | 全局 Experience ledger + index | 跨 TD 匹配、采纳、使用、成功/失败回写和降权 |
| 交互测试 | CLI 单轮协议 | 提示、输入、转换、落盘和退出码 |
| 场景测试 | 完整 TD 生命周期 | 主链、恢复、中断和最终验收 |

## 3. 测试约定

### 3.1 优先级

- `P0`：原型必须通过，否则不能开始交互验证；
- `P1`：应在首个原型完成前通过；
- `P2`：可在原型稳定后补充。

### 3.2 通用断言

除非用例另有说明，每次成功转换都应同时满足：

1. `state.json.state` 等于目标状态；
2. `revision` 单调递增一次；
3. `updated_at` 不早于操作开始时间；
4. `operation.jsonl` 新增一条已接受记录；
5. `event.jsonl` 新增一条状态转换记录；
6. 两条日志使用相同的 `event_id` 关联；
7. 重新加载 TD 后状态和关键 Context 与内存一致。

每次被拒绝的操作都应满足：

1. 当前状态不变；
2. 业务 Context 不发生部分更新；
3. `operation.jsonl` 记录拒绝原因和 Guard 结果；
4. `event.jsonl` 不记录成功状态转换；
5. 恢复预算不被无故消耗。

## 4. 基础测试数据

### 4.1 合法 Target

```json
{
  "positive": ["生成一个包含标题的文本产物"],
  "negative": ["不得修改工作目录以外的文件"],
  "acceptance_criteria": [
    {
      "criterion_id": "tc_001",
      "description": "产物存在且包含指定标题",
      "required": true
    }
  ]
}
```

### 4.2 合法 Observation

```json
{
  "facts": [
    {
      "fact_id": "f_001",
      "description": "目标目录可写",
      "source_type": "human_input",
      "source_ref": null
    }
  ],
  "unknowns": []
}
```

### 4.3 合法 Estimate

```json
{
  "verdict": "feasible",
  "risks": [],
  "cost": {"max_attempts": 3},
  "information_gaps": []
}
```

### 4.4 两步 Action Plan

```json
{
  "plan_id": "plan_001",
  "version": 1,
  "actions": [
    {
      "action_id": "a_001",
      "objective": "创建文本产物",
      "depends_on": [],
      "instruction": "创建包含正文的文本",
      "assertions": [
        {
          "assertion_id": "as_001",
          "description": "产物存在",
          "required": true
        }
      ],
      "max_attempts": 2,
      "status": "pending"
    },
    {
      "action_id": "a_002",
      "objective": "写入标题",
      "depends_on": ["a_001"],
      "instruction": "在产物首行写入指定标题",
      "assertions": [
        {
          "assertion_id": "as_002",
          "description": "首行包含指定标题",
          "required": true
        }
      ],
      "max_attempts": 2,
      "status": "pending"
    }
  ]
}
```

## 5. 状态图与事件测试

| ID | 优先级 | 用例 | 前置状态 | 操作 | 核心断言 |
|---|---|---|---|---|---|
| `SM-001` | P0 | 初始状态 | 新建 TD | 加载机器 | 状态为 `idle`，可用事件包含 `start` |
| `SM-002` | P0 | 启动 TD | `idle` | 发送 `start` | 进入 `targeting` |
| `SM-003` | P0 | 非法事件 | `idle` | 发送 `plan_accepted` | 抛出可识别的 TransitionError，状态不变 |
| `SM-004` | P1 | 终态不可继续 | `succeeded` | 发送任意业务事件 | 全部拒绝，状态保持 `succeeded` |
| `SM-005` | P1 | 取消非终态 TD | 任意可取消状态 | 发送 `cancel` | 进入 `cancelled`，记录用户取消原因 |
| `SM-006` | P1 | 取消终态 TD | `failed` | 发送 `cancel` | 被拒绝，终态不变 |
| `SM-007` | P0 | 状态分支事件唯一 | `checking_action` | 查询可用事件 | 使用 `advance_action`、`actions_completed`，不存在依赖同名事件 Guard 分支的定义 |

## 6. Targeting 测试

| ID | 优先级 | 用例 | 输入或故障 | 期望路径 | 核心断言 |
|---|---|---|---|---|---|
| `TG-001` | P0 | 合法 Target | 使用 4.1 数据 | `targeting → observing` | `target_accepted` 成功；Target revision 为 1 |
| `TG-002` | P0 | 缺少正向目标 | `positive=[]` | 不发生状态转换 | 操作结果为 `target_rejected`；不消耗恢复预算；无 event 记录 |
| `TG-003` | P0 | 缺少反向边界 | `negative=[]` | 不发生状态转换 | 返回字段级错误；不存在部分 Target 更新 |
| `TG-004` | P0 | 缺少验收标准 | `acceptance_criteria=[]` | 不发生状态转换 | 拒绝原因指向验收标准 |
| `TG-005` | P1 | 重复无效输入 | 连续提交同一无效 Target | 保持 `targeting` | 两条 rejected operation；预算仍为初值 |
| `TG-006` | P0 | 目标存在歧义 | 标记缺少必要选择 | `targeting → waiting_human` | `return_to=targeting`；保存人工问题 |
| `TG-007` | P0 | 人工补充目标信息 | `waiting_human` 且 `return_to=targeting` | `waiting_human → targeting` | 使用 `target_input_received`；保存答复；创建新 revision |
| `TG-008` | P0 | Targeting 执行异常 | 注入 `execution_error` | `targeting → recovering` | 使用 `target_failed`；预算消耗一次；保存异常 |
| `TG-009` | P0 | Targeting 超时 | 注入 `timeout` | `targeting → recovering` | cause 为 `timeout`，包含 duration |
| `TG-010` | P0 | 重试 Targeting | 上一步进入 `recovering` | 发送 `retry_targeting` | 返回 `targeting`；创建新的 Attempt |
| `TG-011` | P1 | 错误类型不混淆 | 缺少验收标准 | 不得进入 `recovering` | 输入校验错误不计为执行异常 |
| `TG-012` | P1 | 人工答复事件不匹配 | `waiting_human`，`return_to=recovering` | 发送 `target_input_received` | Guard 拒绝，保持 `waiting_human` |

## 7. Observe 与 Estimate 测试

| ID | 优先级 | 用例 | 前置条件/输入 | 操作 | 核心断言 |
|---|---|---|---|---|---|
| `OE-001` | P0 | 合法 Observation | `observing` + 4.2 数据 | `observation_accepted` | 进入 `estimating` |
| `OE-002` | P0 | 空事实 Observation | `facts=[]` | 提交 Observation | Guard 拒绝，保持 `observing` |
| `OE-003` | P1 | 事实无来源 | fact 缺少 `source_type` | 提交 Observation | Guard 拒绝并指出具体 fact |
| `OE-004` | P0 | Estimate 可行 | `estimating` + 4.3 数据 | `estimate_accepted` | 进入 `deciding` |
| `OE-005` | P0 | Estimate 信息不完整 | 缺少 risks 或 cost | `estimate_accepted` | Guard 拒绝，保持 `estimating` |
| `OE-006` | P0 | Estimate 判定不可行 | `verdict=not_feasible` | 提交 Estimate | 不得进入 `deciding`；进入待确认的不可行处理路径 |
| `OE-007` | P1 | Estimate 有信息缺口 | `information_gaps` 非空 | 提交 Estimate | 不得误判为可直接进入 Decide |

`OE-006` 的精确目标状态依赖设计稿中“Estimate 不可行路径”的最终决策。在该决策确认前，此用例标记为待定，但“不得进入 deciding”是固定断言。

## 8. Decide 与 Action 图测试

| ID | 优先级 | 用例 | Plan 变化 | 核心断言 |
|---|---|---|---|---|
| `DP-001` | P0 | 接受合法两步 Plan | 使用 4.4 数据 | 进入 `acting`，当前 Action 为 `a_001` |
| `DP-002` | P0 | 空 Action 列表 | `actions=[]` | Guard 拒绝，保持 `deciding` |
| `DP-003` | P0 | Action ID 重复 | 两个 `a_001` | Guard 拒绝 |
| `DP-004` | P0 | 依赖不存在 | `depends_on=["a_999"]` | Guard 拒绝并指出无效依赖 |
| `DP-005` | P0 | Action 图存在环 | `a_001 → a_002 → a_001` | Guard 拒绝 |
| `DP-006` | P0 | Action 缺少断言 | `assertions=[]` | Guard 拒绝 |
| `DP-007` | P1 | Action 最大尝试次数非法 | `max_attempts=0` | 数据校验拒绝 |
| `DP-008` | P1 | Plan 版本更新 | Recovery 后 replan | 新 `plan_id/version` 生效，旧 Plan 保留且标记 superseded |
| `DP-009` | P1 | 选择依赖未满足的 Action | `a_001` 未完成时选择 `a_002` | 操作被拒绝，游标不变 |

## 9. Act 与双层 Check 测试

| ID | 优先级 | 用例 | 前置条件/操作 | 期望路径 | 核心断言 |
|---|---|---|---|---|---|
| `AC-001` | P0 | 提交 Action 结果 | `acting`，当前 `a_001` | `action_submitted` | 进入 `checking_action`，产生 Attempt |
| `AC-002` | P0 | Action 结果缺失 | 当前 Attempt 无结果 | `action_submitted` | Guard 拒绝，保持 `acting` |
| `AC-003` | P0 | 非末尾 Action 通过 | `a_001` 断言全部通过 | `advance_action` | 返回 `acting`，当前 Action 变为 `a_002` |
| `AC-004` | P0 | 末尾 Action 通过 | `a_002` 断言全部通过 | `actions_completed` | 进入 `checking_target` |
| `AC-005` | P0 | 必要 Action 断言失败 | required assertion=false | `action_failed` | 进入 `recovering`，保存失败证据 |
| `AC-006` | P1 | 非必要断言失败 | optional assertion=false | 根据策略继续 | 不得自动视为必要断言失败 |
| `AC-007` | P0 | Action 全部通过但 Target 失败 | 在 `checking_target` 提交 failed | `target_failed` | 进入 `recovering`，不得进入 `succeeded` |
| `AC-008` | P0 | Target Check 通过 | 全部必要 criterion=true | `target_passed` | 进入 `succeeded` |
| `AC-009` | P0 | 禁止 Action 直达成功 | `checking_action` | 尝试 `target_passed` | 非法事件，状态不变 |
| `AC-010` | P1 | Check 证据关联 | Action/Target Check 带 evidence refs | 提交检查 | 引用存在且哈希匹配 |

## 10. Recovery 与预算测试

| ID | 优先级 | 用例 | 前置条件/操作 | 核心断言 |
|---|---|---|---|---|
| `RC-001` | P0 | 重试当前 Action | `recovering` + 可用预算 | `retry_action → acting`；Attempt ID 更新 |
| `RC-002` | P0 | Action 尝试次数耗尽 | 当前 Action 达到 `max_attempts` | `retry_action` 被 Guard 拒绝 |
| `RC-003` | P0 | TD 总预算耗尽 | `retry_count=retry_budget` | 不能继续重试，可 `give_up` |
| `RC-004` | P0 | 重新规划 | `recovering` 发送 `replan` | 进入 `deciding`，旧 Plan 不被覆盖 |
| `RC-005` | P0 | 重新观察 | `recovering` 发送 `reobserve` | 进入 `observing`，保留失败上下文 |
| `RC-006` | P0 | 升级人工 | `recovering` 发送 `escalate` | 进入 `waiting_human`，`return_to=recovering` |
| `RC-007` | P0 | 人工答复恢复问题 | 上一步状态 | `recovery_input_received → recovering` |
| `RC-008` | P0 | 放弃任务 | `recovering` 发送 `give_up` | 进入 `failed`，保存最终失败原因 |
| `RC-009` | P1 | 非恢复状态不能重试 | `acting` 发送 `retry_action` | 非法事件，状态不变 |
| `RC-010` | P1 | 重试预算原子更新 | 模拟保存失败 | 预算与状态要么同时提交，要么均不提交 |

## 11. Pause 与人工等待测试

| ID | 优先级 | 用例 | 操作 | 核心断言 |
|---|---|---|---|---|
| `PH-001` | P0 | 暂停业务阶段 | 在 `deciding` 发送 `pause_from_deciding` | 进入 `paused`，`paused_from=deciding` |
| `PH-002` | P0 | 恢复原阶段 | `paused` 发送 `resume_to_deciding` | 返回 `deciding`，清理 `paused_from` |
| `PH-003` | P0 | 恢复事件不匹配 | `paused_from=acting` 时发送 `resume_to_deciding` | Guard 拒绝，保持 `paused` |
| `PH-004` | P1 | Idle 不可暂停 | `idle` 请求 pause | 操作被拒绝 |
| `PH-005` | P0 | 人工等待跨 Session | 保存 `waiting_human` 后重启 | 状态、问题和 `return_to` 完整恢复 |
| `PH-006` | P1 | 暂停跨 Session | 保存 `paused` 后重启 | `paused_from` 完整恢复，可继续 resume |

## 12. 持久化与日志测试

| ID | 优先级 | 用例 | 故障或操作 | 核心断言 |
|---|---|---|---|---|
| `PS-001` | P0 | 保存并恢复状态 | 在每个核心状态保存后重载 | 精确恢复状态与 Context |
| `PS-002` | P0 | 不从默认初态恢复 | 保存 `checking_action` 后新建 Machine | 状态仍为 `checking_action`，不是 `idle` |
| `PS-003` | P0 | 原子写入快照 | 写入中模拟进程终止 | 原文件仍可解析，不出现半个 JSON |
| `PS-004` | P0 | JSONL 追加 | 连续执行多个事件 | 旧日志不被覆盖，每行可独立解析 |
| `PS-005` | P0 | Event/Operation 关联 | 执行一次成功转换 | 两类日志包含相同 `event_id` |
| `PS-006` | P0 | 被拒绝操作留痕 | 发送非法事件 | 只有 rejected operation，无成功 event |
| `PS-007` | P1 | Evidence 完整性 | 保存证据文件 | 日志相对路径可解析，内容哈希一致 |
| `PS-008` | P1 | 非法 Evidence 路径 | 使用 `../` 越界路径 | 拒绝写入或引用 |
| `PS-009` | P1 | Revision 并发冲突 | 用旧 revision 保存 | 检测冲突，不覆盖新状态 |
| `PS-010` | P1 | 日志尾部损坏 | 最后一行是不完整 JSON | 已完成记录仍可读取，并报告尾部异常 |
| `PS-011` | P1 | Session 切换 | 结束 ss_001，创建 ss_002 | TD 状态连续，Session 记录独立 |

## 13. CLI 交互测试

| ID | 优先级 | 用例 | 输入 | 核心断言 |
|---|---|---|---|---|
| `CLI-001` | P0 | 展示当前状态 | 打开现有 TD | 展示状态、所需字段和合法事件 |
| `CLI-002` | P0 | 单轮成功提交 | 合法 JSON | 完成一次转换、落盘并展示下一步 |
| `CLI-003` | P0 | JSON 语法错误 | 不完整 JSON | 提示解析位置，状态不变 |
| `CLI-004` | P0 | Schema 错误 | 类型错误字段 | 提示字段路径，状态不变 |
| `CLI-005` | P1 | 用户中断 | 输入 EOF/Ctrl-C | 安全结束 Session，不修改业务状态 |
| `CLI-006` | P1 | 加载不存在 TD | 未知 td_id | 明确报错，不隐式创建新 TD |
| `CLI-007` | P1 | 创建新 TD | 合法 user_thread_id | 创建唯一 td_id，状态为 `idle` |

## 14. 异常经验测试

| ID | 优先级 | 用例 | 操作 | 核心断言 |
|---|---|---|---|---|
| `EX-001` | P0 | 记录异常出现 | 产生结构化异常 | 追加 `exception_observed`，包含 phase、cause 和 source refs |
| `EX-002` | P0 | 记录处置开始 | 执行恢复策略 | 追加 `treatment_started`；`use_count + 1` |
| `EX-003` | P0 | 成功处置形成经验 | Check 通过 | 追加 `treatment_succeeded`；`success_count + 1` |
| `EX-004` | P0 | 失败处置形成经验 | Check 失败 | 追加 `treatment_failed`；`failure_count + 1`，记录不被删除 |
| `EX-005` | P0 | 匹配近似场景 | 提交相似异常签名 | 返回候选、相似度和适用条件；`match_count + 1` |
| `EX-006` | P0 | Agent 采纳经验 | 对候选作出 adopted 决定 | 记录理由与置信度；`adopt_count + 1` |
| `EX-007` | P0 | Agent 拒绝经验 | 对候选作出 rejected 决定 | 保存拒绝理由；不增加 adopt/use count |
| `EX-008` | P0 | 采纳但未执行 | adopted 后取消 | adopt count 增加，use count 不增加 |
| `EX-009` | P0 | 使用成功经验 | 采纳、执行、Check 通过 | use/success count 各增加一次 |
| `EX-010` | P0 | 使用失败经验 | 采纳、执行、Check 失败 | use/failure count 各增加一次 |
| `EX-011` | P0 | 失败经验减少探索 | 两个同样相似的策略中一个反复失败 | 失败策略排序降低，并向 Agent展示失败证据 |
| `EX-012` | P1 | 环境差异避免硬禁止 | 失败经验环境与当前环境不同 | 仍可成为低权候选，不被永久屏蔽 |
| `EX-013` | P0 | 多次处置分别留痕 | 同一异常先失败后成功 | 两次 Attempt 结果均保留，成功不覆盖失败 |
| `EX-014` | P0 | 计数更新幂等 | 重放同一 experience event | 所有计数只更新一次 |
| `EX-015` | P1 | 重建经验索引 | 删除投影后重放 JSONL | match/adopt/use/success/failure 统计一致 |
| `EX-016` | P1 | 经验可追溯 | 打开任意经验 | 能回到 TD、Session、事件和证据 |
| `EX-017` | P1 | 敏感信息脱敏 | 异常包含凭证 | 经验记录不保存原始凭证 |
| `EX-018` | P0 | 跨 TD 复用经验 | TD-1 写入经验，TD-2 提交近似异常 | TD-2 能在授权作用域内匹配 TD-1 的经验 |
| `EX-019` | P0 | 经验作用域隔离 | 在未授权作用域创建相似经验 | 当前 TD 无法检索或引用该经验 |

## 15. 端到端场景

### `E2E-001`：最短成功路径（P0）

1. 创建 TD；
2. `idle → targeting → observing → estimating → deciding`；
3. 提交单 Action Plan；
4. `acting → checking_action → checking_target → succeeded`；
5. 重启程序并加载 TD。

断言：最终仍为 `succeeded`；所有阶段产出、日志和证据链可追溯。

### `E2E-002`：Target 信息不足后继续（P0）

1. 进入 `targeting`；
2. 发现目标歧义，进入 `waiting_human`；
3. 关闭程序并开启新 Session；
4. 提交人工答复，返回 `targeting`；
5. 接受新 Target revision 并完成任务。

断言：人工问题和答复未丢失；旧 Target 草稿仍可追溯；没有消耗恢复预算。

### `E2E-003`：Action 失败后重试成功（P0）

1. 完成 TOE-D 并执行 `a_001`；
2. Action Check 失败，进入 `recovering`；
3. 发送 `retry_action`，产生新 Attempt；
4. 第二次 Action Check 通过；
5. Target Check 通过。

断言：两个 Attempt 均保留；预算只消耗一次；最终成功来自 Target Check。

### `E2E-004`：Action 完成但目标未达成（P0）

1. 所有 Action Check 通过；
2. Target Check 失败；
3. 选择 `replan`，生成 Plan v2；
4. 执行新增 Action；
5. Target Check 通过。

断言：Plan v1 与 v2 均可追溯；不能因 Action Check 全部通过而提前成功。

### `E2E-005`：恢复预算耗尽（P0）

1. 连续制造可恢复执行异常；
2. 使用完 TD 恢复预算；
3. 再次尝试重试；
4. 选择 `give_up`。

断言：超预算重试被拒绝；最终进入 `failed`；失败原因和所有尝试完整保留。

### `E2E-006`：失败后请求人类确认（P0）

1. Action Check 失败并进入 `recovering`；
2. 自动恢复尝试失败或风险超过自动授权范围；
3. `escalate → waiting_human`；
4. 向人类展示失败原因、已尝试路径、证据、候选方案和剩余预算；
5. 人类选择 `replan`；
6. 新 Session 中通过 `recovery_input_received` 返回 `recovering`；
7. 进入 `deciding` 生成新 Plan，执行后成功。

断言：人工决定是结构化数据；等待可跨 Session 恢复；原失败、人工决策和新方案结果形成完整经验。

### `E2E-007`：失败经验减少重复探索（P0）

1. TD-1 的异常先使用策略 A，Check 失败；
2. 改用策略 B，Check 成功；
3. TD-2 出现近似异常；
4. 系统匹配到策略 A、B 的历史；
5. Agent 根据相似度、适用条件和成败统计采纳策略 B；
6. 执行策略 B 并回写最终结果。

断言：策略 A 的失败记录仍可见但排序降低；Agent 的采纳理由留痕；策略 B 的 match/adopt/use/success 计数正确更新。

## 16. 测试实施顺序

1. 先实现 `SM-*`、`TG-*` 和 Guard 单元测试；
2. 再实现主链 `OE-*`、`DP-*`、`AC-*`；
3. 然后实现恢复、暂停和人工等待测试；
4. 状态机稳定后加入持久化、日志故障注入和经验账本测试；
5. 最后实现 CLI 与七个端到端场景。

首轮原型的最低门槛是：全部 `P0` 用例通过，且没有跳过 `TG-*`、`AC-*`、`RC-*` 或 `PS-*` 中的 P0 用例。

## 17. 尚待设计确认的测试点

以下测试无法在状态机设计确认前固定最终断言：

1. Estimate 为 `not_feasible` 时进入哪个状态；
2. optional Action assertion 失败是否允许继续；
3. 请求人工输入后，除 Targeting 与 Recovering 外的其他阶段如何返回；
4. 达到恢复预算后是自动 `failed`，还是必须显式 `give_up`。

这些项目确认后，应删除“待定”描述并补成确定性测试，不能在实现中临时决定。
