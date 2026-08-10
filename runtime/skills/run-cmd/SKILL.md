---
name: run-cmd
description: 按需异步执行 Bash 命令并通过持久化 job_id 查询输出或终止任务。用于 SSH、Git、Docker、系统检查、测试、构建和其他需要真实命令行的场景；当现有专用 Skill 无法完成命令执行时使用。
compatibility: Requires Python 3 and bash. Commands run with the current OS user's permissions.
metadata:
  version: "1.0"
---

# run-cmd

通过 `scripts/run_cmd.py` 启动、查询和终止后台 Bash 任务。命令在独立进程组中运行，启动调用会立即返回 `job_id`，不会等待命令完成。

## 约束

- 这是任意命令执行能力。只执行 Target 和当前阶段明确允许的操作。
- Observe 和 Check 默认只运行读取、探测及验证命令；修改外部状态的命令属于 Act。
- 不把密码、私钥正文、API Key 或 Token直接放入命令字符串。优先使用本机 SSH 配置、环境变量或当前 User Thread 的 credentials 引用。
- 不使用忙轮询。启动后根据任务预计时长查询状态；任务仍在运行时保留 `job_id`。
- 只有 `status=completed` 且 `exit_code=0` 才能作为命令成功的证据。
- `status=failed`、`killed` 或非零退出码都必须按失败处理，并保留 stderr。

## 启动

调用 `run_skill_script`：

```json
{
  "skill_name": "run-cmd",
  "script": "scripts/run_cmd.py",
  "arguments": [
    "start",
    "--command",
    "ssh root@45.126.120.34 'uname -a; cat /etc/os-release'",
    "--cwd",
    "/tmp"
  ],
  "timeout": 10
}
```

`start` 返回 `job_id` 和 `status=running`。这里的 `timeout` 只限制启动脚本自身，不限制已经脱离运行的后台命令。

## 查询状态和增量输出

```json
{
  "skill_name": "run-cmd",
  "script": "scripts/run_cmd.py",
  "arguments": [
    "status",
    "--job-id",
    "cmd-1234abcd",
    "--stdout-offset",
    "0",
    "--stderr-offset",
    "0"
  ]
}
```

响应包含下一次应使用的 `stdout_offset` 和 `stderr_offset`，只读取新增输出。

## 终止

```json
{
  "skill_name": "run-cmd",
  "script": "scripts/run_cmd.py",
  "arguments": ["kill", "--job-id", "cmd-1234abcd"]
}
```

终止操作会向整个后台进程组发送 `SIGTERM`。
