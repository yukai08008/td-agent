# CLI 准 Web 服务与持久化设计

## 1. 设计边界

TD Agent CLI 按一个本地准 Web 服务设计。每次用户输入视为一个请求，具有独立的
`request_id`，并关联唯一的 User Thread、Session 和当前 TD。运行数据分成三类：

1. **运行访问日志**：服务是否被访问、响应耗时和结果；不保存对话正文和凭据。
2. **User Thread 业务数据**：状态、事件、操作、会话轨迹和产物，可作为容器数据卷挂载。
3. **Credentials**：独立于业务数据，默认只引用本机已有凭据；显式托管时按 User Thread 隔离。

访问日志只保留 7 天；业务事件和操作日志是审计与恢复依据，生命周期跟随 User Thread，
不能按 7 天滚动删除。

## 2. 目标目录结构

```text
~/.td-agent/
├── logs/
│   └── access.log                         # 服务访问日志，每日滚动，保留 7 天
├── data/
│   ├── user-threads/
│   │   └── <user_thread_id>/
│   │       ├── meta.json                  # 非敏感、可读的线头元数据
│   │       ├── state.json                 # 线头级调度快照和活动 TD 指针
│   │       ├── logs/
│   │       │   ├── event.jsonl            # 里程碑和状态转换
│   │       │   └── opr.jsonl              # 模型、工具、校验、恢复等操作明细
│   │       ├── trace/sessions/
│   │       ├── artifacts/
│   │       └── td/<td_id>/state.json      # 根 TD 或子 TD 的独立状态快照
│   └── experience/
│       ├── ledger.jsonl
│       └── index.json
└── credentials/
    └── user-threads/<user_thread_id>/
```

`~/.td-agent` 是统一安装后的固定运行目录。普通 CLI 不提供数据路径参数；容器或版本兼容测试
可以使用 `TD_AGENT_HOME`、`TOE_DAC_DATA` 和 `TOE_DAC_LOG_DIR` 环境变量覆盖，并将对应目录挂载为卷。

### 为什么同时需要两个 state

- User Thread 的 `state.json` 保存当前 Session、活动根 TD、整体 revision 和调度信息；
- `td/<td_id>/state.json` 保存每个父/子 TD 自己的 TOE-DAC 状态。

如果只保留一个 `state.json`，层级 TD 并发或断点恢复时会互相覆盖。Thread 下的 `event.jsonl`
和 `opr.jsonl` 采用聚合日志，每条记录必须包含 `td_id`、`session_id` 和 `request_id`，因此仍可按
任意 TD 回放。

## 3. 请求与访问日志

一次输入的逻辑链路为：

```text
request_id
  → user_ask | user_answer
  → conversation route
  → TOE-DAC phase / local control
  → agent_ask | agent_answer
```

`access.log` 使用普通单行日志格式：

```text
2026-08-09 16:41:19 INFO request_id=req_xxx user_thread=ut_xxx session=ss_xxx type=agent_ask duration_ms=12131.2 status=ok
```

字段至少包括：

- 时间、`request_id`、`user_thread_id`、`session_id`；
- `type`：`user_ask`、`user_answer`、`agent_ask`、`agent_answer`；
- `duration_ms` 和 `status`。

访问日志不得记录消息正文、模型原始响应、Authorization Header 或环境变量。原始模型响应属于
业务证据，只能写入当前 Session 的 trace，并由 `opr.jsonl` 保存相对引用。

当前 POC 使用单进程的每日滚动文件。变成多进程服务时应改为单一日志写入器或 stdout 日志采集，
不能让多个进程直接轮转同一个文件。

## 4. Session

一个明确需求对应一个 User Thread；Session 是围绕该需求展开的一次持久化 `toe-dac-loop`。
CLI 多次启动只是反复 attach 同一个 Session，不会自动创建新 Session。只有显式执行
`toe-dac session new` 才创建另一个 Session。目标 Session 名称为：

```text
sess-<8位随机码>-<YYYYMMDD_HHMMSS>
```

时间戳采用东八区本地时间，便于人工检查；随机码避免同秒冲突。Session ID 创建后不可修改。`/quit` 将 Session 标为
`detached`，再次执行 `continue` 后恢复为 `active`；只有 loop 成功、失败或取消时才写入 `ended_at`。
普通问候、帮助、状态查询等对话控制输入不得推进状态机。

## 5. meta.json

`meta.json` 只保存可读但非敏感的信息，例如：

- User Thread 标题、目标摘要、创建与更新时间；
- 主机标识、机器地址、服务名称、运行环境；
- 创建版本、最近 Review 时间、最近 Review 结论；
- 根 TD、Session 和 Artifact 索引。

Review 可以更新当前摘要，但更新动作必须同时写入 `event.jsonl`，保留旧值摘要和 revision，不能只做
无痕覆盖。IP、主机名虽然允许明文保存，仍应支持配置关闭或脱敏。

## 6. Credentials 隔离

默认优先从操作系统环境、系统 Keychain 或用户明确指定的本机文件读取凭据，不复制进数据卷。
只有用户明确要求 TD Agent 托管时，才写入：

```text
~/.td-agent/credentials/user-threads/<user_thread_id>/
├── manifest.json                 # 凭据名称、来源和用途，不含密钥值
└── <credential files>
```

必须执行以下约束：

- credentials 根目录权限 `0700`，凭据文件权限 `0600`；
- 解析路径后校验其仍位于当前 `user_thread_id` 目录，拒绝 `..`、软链接越界和跨线头读取；
- Executor 只获得当前 Action 明确授权的凭据引用，不能获得 credentials 根目录浏览权限；
- 容器只读挂载当前 User Thread 的凭据子目录，不能挂载整个 credentials 目录；
- 日志、trace、artifact 和异常经验均不得保存凭据值；
- 删除 User Thread 业务数据时不默认删除凭据，凭据删除必须单独确认并记录。

## 7. 一致性与准 Web 服务要求

从 CLI 演进为常驻服务前需要补齐：

- 每个 User Thread 单写锁，防止两个 Session 同时更新同一 revision；
- `state.json` 原子替换，以及基于 revision 的乐观并发校验；
- `request_id` 幂等处理，重复请求不能重复执行 Action；
- 先追加操作事实、再提交状态快照的恢复协议；
- trace/artifact 哈希和相对路径校验；
- 数据根目录、访问日志目录、凭据目录分别支持配置和容器挂载。

## 8. 兼容迁移

v0.4.x POC 的 `threads/<id>/td/<id>/...` 数据继续可读。v0.5.0 迁移分三步：

1. 先增加输入路由、`request_id` 和全局 `logs/access.log`，不移动旧数据；
2. 新建 Thread 只写入 `user-threads/`，旧结构保持双读；
3. 使用 `toe-dac storage migrate` 预检，再用 `toe-dac storage migrate --execute`
   构建临时目录并校验日志数量、证据数量和 Artifact SHA-256 后原子切换。旧目录保留，不在启动时静默搬迁。

## 9. Skills 与 Persona

全局运行时内容位于：

```text
~/.td-agent/
├── skills/
│   ├── index.md
│   └── <skill-name>/SKILL.md
└── persona/
    ├── active.json
    ├── blue/system.md
    └── green/system.md
```

- Skill 采用 Claude 风格的 `SKILL.md`，必须具有 `name` 和 `description` YAML frontmatter；
- `index.md` 决定启用状态、加载顺序和所需的可执行 capability；
- 当前策略是 `all_sessions`，每次 attach Session 时加载一次不可变快照；
- Persona 由 `active.json` 指向 blue 或 green，切换后在下一次 Session attach 时生效；
- 初始化只创建缺失文件，不覆盖用户编辑的 Skill、索引或 Persona；
- Skill 是方法说明，不等价于 Tool、Credential 或 Grant。缺少可执行 capability 时必须如实报告。
