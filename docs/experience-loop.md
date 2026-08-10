# TOE-DAC 异常经验闭环

## 目标

经验机制把一次异常保存为可回溯、可检索、可由模型选择、可用结果更新的工程记录：

```text
异常出现
→ 创建结构化签名
→ 记录每次 Treatment
→ 成功或失败
→ 近似场景检索
→ 模型 Adopt / Reject
→ 再次记录使用结果
```

失败经验用于避免重复无效探索；成功经验用于提供已经验证的候选策略。历史经验不能替代当前事实，
也不能自动扩大权限。

## 持久化结构

```text
~/.td-agent/data/experience/
├── ledger.jsonl   # append-only 事实源
└── index.json     # 可由 ledger 完整重建的检索投影
```

主要事件：

| 事件 | 含义 |
| --- | --- |
| `exception_observed` | 首次观察到异常，保存签名与原始来源引用 |
| `experience_matched` | 新异常检索到候选经验 |
| `experience_adopted` / `experience_rejected` | 模型明确采纳或拒绝候选 |
| `treatment_started` | 一次具体处理开始 |
| `treatment_succeeded` / `treatment_failed` | 处理结果及证据 |
| `outcome_recorded` | TD 的最终成功或失败结果 |
| `resolution_recorded` | 代码修复、版本、提交和回归测试等已验证方案 |
| `experience_classified` | 对历史经验补充作用域与稳定错误签名 |

每次 Treatment 有独立 `treatment_id`。经验累计 `match/adopt/use/success/failure/effectiveness`
统计，同时保存 operation、evidence、artifact、Session、TD 和 User Thread 引用。

## 作用域与隐私

- `thread`：只在原 User Thread 内检索，适合业务上下文和用户私有环境经验；
- `system`：可跨 User Thread 检索，适合模型协议、控制规则、工具和运行时缺陷；
- system 候选传给模型时只提供去敏签名、统计和解决方案，不传原始异常正文与来源路径；
- 凭证不进入经验 Ledger，仍受 User Thread credentials 隔离约束。

## 检索与采用

阶段模型收到 `experience_candidates` 后必须在结构化输出中返回：

```json
{
  "experience_decisions": [
    {
      "experience_id": "exp_xxxxxxxx",
      "decision": "adopt",
      "reason": "错误代码、阶段和控制规则一致，且历史方案已验证",
      "confidence": 0.95
    }
  ]
}
```

控制层只接受当前候选集合内的 ID。采纳只是开始一次新的 Treatment；只有当前执行实际成功，
历史经验的 `success_count` 和 `effectiveness` 才会更新。

## 查看与重建

```bash
toe-dac experience list
toe-dac experience list --visibility system --limit 10
toe-dac experience show exp_14777edb
toe-dac experience rebuild
```

`list` 和 `show` 是只读操作，不刷新证据或索引时间。`rebuild` 明确地从 append-only Ledger
重新生成 Index。

## 本次样本

Session `sess-8e470940-20260810_004359` 对应经验 `exp_14777edb` 已完成回填：

- 分类：`system`；
- 错误代码：`plan.screenshot_relocation_conflict`；
- 原始结果：失败；
- 解决方案：`v0.6.1`，commit `b51c52d`；
- 回归测试：`test_report_plan_that_forbids_screenshot_relocation_is_valid`；
- 当前统计：一次失败、一次成功，effectiveness 为 `0.5`；
- 两次 operation、evidence.jsonl 和 failure Artifact 均可由经验直接回溯。
