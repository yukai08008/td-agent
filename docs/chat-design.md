# 持续交互会话设计

## 对象关系

```text
UserThread
├── messages.jsonl              跨 Session 的自然语言时间线
├── thread.json                 根 TD 与 session_ids
├── sessions/*.json            多次进入和退出形成的会话
└── td/                         需求内部的父子 TD
    └── <td_id>/
        ├── state.json
        └── event/operation log
```

- 一个明确的用户需求对应一个 User Thread；
- Session 必须依附于 User Thread，每次进入或恢复该需求时创建一个 Session；
- TD 是 Thread 内部的规划与执行结构，不能用新的 TD 承载另一个用户需求；
- 每条消息关联 `td_id` 和 `session_id`；
- 根 TD 进入终态后保持不可变；新需求必须创建新 Thread；
- Thread 聚合其全部 Session 的消息、事件和产物。

## CLI

```bash
toe-dac continue --thread ut_demo --model glm-5
```

自然语言输入由 Conversation Controller 根据当前 TD 状态解释。控制命令使用 `/` 前缀：

- `/status`：当前 Thread、TD、Session 和状态；
- `/history`：最近消息；
- `/pause`、`/resume`、`/cancel`；
- `/quit`：退出 CLI，不结束 TD。

## 自动推进边界

收到一条用户消息后，Controller 在预算内自动推进：

```text
Target → Observe → Estimate → Decide
```

直到：

1. 阶段缺少关键信息，进入 `waiting_human`；
2. Plan 已建立，进入 `acting`，等待受限 Executor；
3. 发生异常进入 `recovering`；
4. TD 到达终态。

模型只提交当前阶段结构，Controller 校验并驱动状态机；模型不能直接写状态。
