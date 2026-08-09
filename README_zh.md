# TD Agent

[English](README.md)

基于 TOE-DAC 控制协议构建的可持久化、可持续交互 Agent CLI。TOE-DAC 代表 Target、Observe、Estimate、Decide、Act 和 Check。

一个明确的用户需求对应一个 **User Thread**；每次重新进入该需求都会创建一个 **Session**；规划和执行由可持久化、可追踪的 **TD 实例**表达。

## 一键安装

支持 Linux 和 macOS：

```bash
curl -fL --connect-timeout 10 --retry 3 --progress-bar \
  https://raw.githubusercontent.com/yukai08008/td-agent/v0.4.3/install.sh | bash
```

安装脚本会在需要时安装 [uv](https://docs.astral.sh/uv/)，把 TD Agent 安装为隔离的 uv tool，并在 `~/.config/td-agent/` 初始化当前机器的配置。

安装完成后直接运行：

```bash
toe-dac
```

CLI 启动时会检查是否至少存在一个真正可用的模型配置；如果没有，会自动引导选择模型并输入 API Key。密钥只保存到当前机器的 `~/.config/td-agent/.env.local`。

## 卸载

```bash
curl -fL --connect-timeout 10 --retry 3 --progress-bar \
  https://raw.githubusercontent.com/yukai08008/td-agent/v0.4.3/install.sh | bash -s -- uninstall
```

卸载会保留 `~/.config/td-agent/` 中的配置和本地运行数据。

## 更新

```bash
toe-dac upgrade
```

也可以显式运行安装脚本：

```bash
curl -fL --connect-timeout 10 --retry 3 --progress-bar \
  https://raw.githubusercontent.com/yukai08008/td-agent/v0.4.3/install.sh | bash -s -- update
```

TD Agent 每次启动都会检查更新。检查使用本地缓存和短网络超时，只有发现远端版本更新时才显示提示。

可在 `.env.local` 中控制：

```dotenv
TOE_DAC_UPDATE_CHECK=true
TOE_DAC_UPDATE_CHECK_INTERVAL=86400
TOE_DAC_UPDATE_CHECK_TIMEOUT=1.5
```

设置 `TOE_DAC_UPDATE_CHECK=false` 可以关闭更新检查。

## 运行指定版本

默认直接运行当前安装的 CLI。临时运行指定 Tag、且不替换当前安装版本：

```bash
toe-dac --use-version 0.2.0 --version
toe-dac --use-version 0.2.0 --data ~/.local/share/td-agent-v0.2 new
```

把本机长期固定到指定版本：

```bash
toe-dac upgrade --version 0.2.0
curl -fL --connect-timeout 10 --retry 3 --progress-bar \
  https://raw.githubusercontent.com/yukai08008/td-agent/v0.4.3/install.sh | bash -s -- install 0.2.0
```

公开仓库从 `v0.2.0` 开始支持独立安装。测试可能使用不兼容持久化格式的旧版本时，应通过 `--data` 使用独立数据目录。

## 当前版本

当前稳定版本为 [v0.4.3](https://github.com/yukai08008/td-agent/releases/tag/v0.4.3)，安装时持续显示包来源、依赖、缓存位置、uv 阶段输出和等待心跳，并支持启动配置引导和持久化交互。

[完整版本记录](versions.md) · [GitHub Releases](https://github.com/yukai08008/td-agent/releases)

## 使用 uv 直接安装

```bash
uv tool install git+https://github.com/yukai08008/td-agent.git
```

如果跳过 `install.sh`，需要根据仓库模板自行创建 `~/.config/td-agent/models.json` 和 `~/.config/td-agent/.env.local`。

## 使用方法

```bash
# 查看版本和系统信息
toe-dac --version

# 为新需求创建 User Thread 和第一个 Session
toe-dac new

# 在新的 Session 中继续最近的需求
toe-dac

# 继续指定的 User Thread
toe-dac continue --thread ut_xxxxxxxx

# 查看 Thread 和 Session
toe-dac threads
toe-dac sessions --thread ut_xxxxxxxx

# 检查当前配置
toe-dac config
toe-dac config --show
toe-dac doctor

# 更新到 GitHub 最新版本
toe-dac upgrade

# 查看全部命令
toe-dac --help
```

持续会话中可通过 `/help` 查看 `/status`、`/history`、`/pause`、`/resume`、`/cancel` 和 `/quit` 等命令。

## 配置

环境变量覆盖顺序：

```text
进程环境 > .env.local > .env > .env.example
```

- `.env.local`：当前机器的真实密钥，不上传 Git。
- `.env`：可上传的项目级占位或通用非敏感配置。
- `.env.example`：完整环境变量模板。
- `config/models.json`：只保存模型元数据和 `apiKeyEnv` 引用，不保存 API Key。

源码运行时读取项目目录配置；一键安装后读取 `~/.config/td-agent/`。每台机器都应独立创建 `.env.local`，不要跨机器复制 GitHub Token、SSH 私钥或运行数据。

## 工作原理

控制器按照六个明确阶段推进任务：

```text
Target → Observe → Estimate → Decide → Act → Check
```

1. **Target**：定义可验证结果、排除项和验收标准。
2. **Observe**：记录有来源的事实和未知信息。
3. **Estimate**：评估可行性、风险、成本和信息缺口。
4. **Decide**：生成包含依赖、断言和预算的行动图。
5. **Act**：执行一个原子行动并记录结构化证据。
6. **Check**：分别检查动作是否完成、目标是否真正实现。

模型只负责提出结构化结果；确定性校验和状态机决定是否接受状态转换。非法输出会留痕，在预算内自动修复，无法恢复时请求人类确认。

## 持久化模型

```text
User Thread（一个明确需求）
├── messages.jsonl
├── sessions/
│   ├── ss_xxxxxxxx.json
│   └── ss_xxxxxxxx.json
└── td/
    └── td_xxxxxxxx/
        ├── state.json
        ├── event.jsonl
        └── operation.jsonl
```

- 新需求创建新的 User Thread。
- 重新进入原需求时，在 Thread 下创建新 Session。
- TD 表达需求内部的规划和执行层级。
- 根 TD 进入终态后，不会把 Thread 自动变成另一个需求的容器。

## 项目结构

```text
td-agent/
├── install.sh              # 一键安装、更新和卸载
├── config/                 # 安全模型注册表
├── docs/                   # 协议、状态机和 E2E 设计
├── src/toe_dac/
│   ├── cli.py              # CLI 命令入口
│   ├── chat_ui.py          # Rich 交互界面
│   ├── conversation.py     # 自然语言会话控制器
│   ├── service.py          # TD 状态机服务
│   ├── storage.py          # Thread、Session、TD 和日志持久化
│   ├── experience.py       # 异常处置经验账本
│   ├── update_check.py     # 远端版本检查
│   └── e2e/                # 可执行原型场景
├── tests/
├── pyproject.toml
└── README.md
```

## E2E 场景

```bash
toe-dac case list
toe-dac --data ./data run LIVE-001 --mode mock
toe-dac --data ./data run LIVE-002 --mode mock
toe-dac --data ./data run LIVE-006 --mode mock
toe-dac --data ./data resume <run_id>
toe-dac --data ./data report <run_id>
```

## 开发

```bash
git clone https://github.com/yukai08008/td-agent.git
cd td-agent
cp .env.example .env.local
chmod 600 .env.local

uv sync
uv run pytest
uv build
```

## 当前范围

当前 POC 已覆盖 Thread/Session 持久化、TOE-DAC 主状态链、结构化确定性校验、Target 自动修复、Action/Target 双层检查、恢复预算、人工中断和异常经验统计。

受限 Action Executor、父子 TD 编排、证据采集和语义经验检索仍在开发。Plan 被接受后，当前交互流程会停在 Executor 边界。
