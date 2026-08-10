# TD Agent 版本记录

这里汇总 TD Agent 的正式版本。每个版本的详细说明同时发布在
[GitHub Releases](https://github.com/yukai08008/td-agent/releases)，可直接在网页查看。

版本号遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。

## 版本总览

| 版本 | 日期 | 版本主题 | 详细说明 |
| --- | --- | --- | --- |
| `v0.7.0` | 2026-08-10 | 异常经验检索、采纳与效果闭环 | [版本说明](docs/releases/v0.7.0.md) |
| `v0.6.1` | 2026-08-10 | 修复截图迁移否定语义误判 | [版本说明](docs/releases/v0.6.1.md) |
| `v0.6.0` | 2026-08-10 | 模型判断与确定性控制层协作 | [版本说明](docs/releases/v0.6.0.md) |
| `v0.5.0` | 2026-08-09 | Storage V2 与可验证迁移 | [版本说明](docs/releases/v0.5.0.md) |
| `v0.4.3` | 2026-08-09 | 安装过程持续显示包状态 | [版本说明](docs/releases/v0.4.3.md) |
| `v0.4.2` | 2026-08-09 | 默认配置随 wheel 本地初始化 | [版本说明](docs/releases/v0.4.2.md) |
| `v0.4.1` | 2026-08-09 | 安装依赖收敛到 agenty 同一量级 | [版本说明](docs/releases/v0.4.1.md) |
| `v0.4.0` | 2026-08-09 | 启动配置引导与可观察安装 | [版本说明](docs/releases/v0.4.0.md) |
| `v0.3.1` | 2026-08-09 | GitHub-first 的结构化版本展示 | [版本说明](docs/releases/v0.3.1.md) |
| `v0.3.0` | 2026-08-09 | 临时运行或固定安装指定版本 | [版本说明](docs/releases/v0.3.0.md) |
| `v0.2.0` | 2026-08-09 | 可公开独立安装与安全配置 | [版本说明](docs/releases/v0.2.0.md) |
| `v0.1.0` | 2026-08-09 | TOE-DAC 交互式原型 | [版本说明](docs/releases/v0.1.0.md) |

## 版本策略

- `v0.x.0`：增加用户可见能力或调整产品行为。
- `v0.x.y`：兼容性修正、文档和发布体验改进。
- Git Tag 固定代码快照，GitHub Release 展示该版本的结构化变化。
- README 只描述当前版本；完整历史以本页和 Releases 为准。

[查看所有 Releases](https://github.com/yukai08008/td-agent/releases) ·
[比较 v0.6.1...v0.7.0](https://github.com/yukai08008/td-agent/compare/v0.6.1...v0.7.0)
