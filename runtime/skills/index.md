---
schema_version: "1.0"
load_policy: progressive
---

# Skills Index

Only skills with a concrete, current purpose belong in this index.

## toe-dac-control

- Path: toe-dac-control/SKILL.md
- Description: Apply TOE-DAC phase boundaries and distinguish action checks from target checks.
- Enabled: true
- Order: 10
- Requires: none
- Phases: all

## agent-browser

- Path: agent-browser/SKILL.md
- Description: 用 agent-browser CLI 完成需要真实浏览器的打开、截图、点击、填表和动态页面提取。
- Enabled: true
- Order: 20
- Requires: cli:agent-browser, cli:node
- Phases: observe

## alex-serp

- Path: alex-serp/SKILL.md
- Description: 按需通过百度 SERP API 搜索中文网页结果，返回标题、摘要和链接。
- Enabled: true
- Order: 30
- Requires: tool:alex_serp_search
- Phases: observe
