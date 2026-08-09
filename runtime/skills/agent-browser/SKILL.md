---
name: agent-browser
description: 用 agent-browser CLI 做浏览器自动化：打开网页、截图、提取内容、点击元素、填表单、多步交互。适用于需要 JS 渲染、真实浏览器交互、登录操作的场景。当用户要求打开网页、截图、填表单、浏览器自动化、提取动态页面内容时触发。
trigger: 当用户提到 agent-browser、浏览器自动化、打开网页、截图、填表单、点击元素、提取动态页面、浏览器交互时触发
---

# agent-browser — 浏览器自动化

用 `agent-browser` CLI（基于 vercel-labs/agent-browser）做浏览器自动化。适用于需要 JS 渲染、真实浏览器交互、截图、表单操作的场景。

上游项目：https://github.com/vercel-labs/agent-browser

## 何时用 / 何时不用

| 需求 | 推荐工具 |
|---|---|
| 抓取静态 HTML / 纯文本 / markdown | WebFetch / curl |
| 调用 JSON / REST API | curl / fetch |
| 渲染 JS 驱动的页面后再读取 | **agent-browser** |
| 点击、输入、登录、多步交互 | **agent-browser** |
| 截图供人工查看 | **agent-browser** |
| 端到端复现用户操作流程 | **agent-browser** |

**规则**：如果 `curl <url>` 已经能拿到目标内容，就不要启动 agent-browser。

## 前置条件

- Node.js 18+
- npm 在 PATH 中
- 约 500MB 磁盘空间（Chromium）
- **PATH 上的 node 必须能正常执行代码**（agent-browser 内部会 spawn Node 子进程）

快速检查：
```bash
node -e "console.log('ok')"
```
如果不打印 `ok`（SIGILL / exit 133 / 任何崩溃），说明 Node 不可用。修复后或用已知可用的 Node：
```bash
PATH="/path/to/working-node/bin:$PATH" agent-browser open <url>
```

## 安装

```bash
npm install -g agent-browser
agent-browser install
```

Linux 缺依赖时：
```bash
agent-browser install --with-deps
```

## 核心概念：Session 模型

`agent-browser` 运行一个**持久化后台 daemon**：

- 第一次 `agent-browser open` 启动 daemon，后续命令自动连接同一 daemon
- 同一 session 内 cookies、localStorage、登录状态跨命令保留
- 多次 `open` 在同一浏览器内导航（不需要在 URL 之间 close）
- `agent-browser close` 结束 daemon，只在**整个任务完成时**调用

** implication**：访问 N 个 URL 的任务 = 每个 URL 一个 `open → snapshot` 链 + 最后一个 `close`，不是 N 个 `open/close` 对。

## 命令序列（必须按此顺序）

1. 打开目标 URL
2. 按需等待页面加载
3. 用 `snapshot` 或 `snapshot -i` 检查页面
4. 与页面交互
5. **完成后 close**

```bash
agent-browser open https://example.com
agent-browser wait --load networkidle
agent-browser snapshot
agent-browser close
```

## 命令参考

| 命令 | 说明 |
|---|---|
| `agent-browser open <url>` | 启动/连接 daemon 并导航到 URL |
| `agent-browser wait --load networkidle` | 等待网络空闲（动态页面后用） |
| `agent-browser snapshot` | 获取页面文本内容 |
| `agent-browser snapshot -i` | 获取带元素 ID 的内容（用于交互） |
| `agent-browser screenshot` | 截图 |
| `agent-browser click <selector>` | 点击元素 |
| `agent-browser type <selector> <text>` | 输入文本 |
| `agent-browser close` | 关闭浏览器并释放资源 |

## 执行原则

- **close 必须在 finally**：任务成功或中途失败，都要 `agent-browser close`，避免僵尸 daemon 和泄漏的 Chromium 进程
- **wait 可选可降级**：`wait --load networkidle` 在永不空闲的 SPA 上会卡住。卡住时降级为 `wait --load load` 或直接 `snapshot`
- **复用 daemon**：同一任务中间步骤不要 close，open 一次、多次导航/交互、最后 close
- **优先用 `snapshot -i` 做交互**：需要点击或输入时，先 `snapshot -i` 拿到交互元素 ID，再操作。不要盲猜 CSS 选择器
- **用 agent-browser 自己的安装通道**：Chromium / runtime / 依赖问题用 `agent-browser install`，不要并行跑 `npx playwright install`（可能拉到不匹配的版本）
- **环境错误快速失败**：第一次 `open` 就失败（daemon / Chromium / SIGILL），不要重试相同命令，先检查 Node 前置条件

## 常用工作流

### 查看网页内容

```bash
agent-browser open https://example.com
agent-browser wait --load networkidle
agent-browser snapshot
agent-browser close
```

### 截图

```bash
agent-browser open https://example.com
agent-browser wait --load networkidle
agent-browser screenshot
agent-browser close
```

### 填表单并提交

先获取交互元素 ID：
```bash
agent-browser open https://example.com/login
agent-browser wait --load networkidle
agent-browser snapshot -i
```

然后交互：
```bash
agent-browser type "#username" "myuser"
agent-browser type "#password" "mypassword"
agent-browser click "#submit"
agent-browser close
```

### 从 JS 渲染页面提取数据

```bash
agent-browser open https://example.com/data
agent-browser wait --load networkidle
agent-browser snapshot
agent-browser close
```

## 平台支持

- macOS ARM64 / x64
- Linux ARM64 / x64
- Windows x64（Windows 11 + PowerShell + Node.js 18+）

## 常见问题

| 症状 | 解决 |
|---|---|
| 安装后 `agent-browser` 命令找不到 | 重启 shell，确认 npm 全局 bin 在 PATH（`npm config get prefix`） |
| Linux 浏览器启动缺系统库 | `agent-browser install --with-deps` |
| daemon 启动失败 / SIGILL / Exit 133 | 检查 Node 前置条件，`node -e "console.log('ok')"` 必须正常 |
| `wait --load networkidle` 卡住 | 降级为 `wait --load load` 或跳过等待直接 `snapshot` |
| Chromium 版本不匹配 | 只用 `agent-browser install`，不要 `npx playwright install` |

## 与其他工具的关系

| Skill / 工具 | 角色 |
|---|---|
| **agent-browser**（本 skill） | 真实浏览器自动化（JS 渲染、交互、截图） |
| `alex-serp` | 轻量级百度 SERP 搜索（标题、摘要和链接） |
| WebFetch / curl | 静态 HTML / API 调用 |
| `playwright-cli` | Playwright 浏览器自动化（另一个 CLI 工具） |
