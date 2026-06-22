---
name: agent-os-experience-eval
display_name: Agent OS · 体验评估
version: 0.1.0
author: hipop
description: Agent OS 的体验质量门——commit 前必跑的 chat smoke 覆盖（每 case 验 HTTP/tool 调用/关键词/反 hallucinate 黑名单），以及 chat 持久化与切页面继承（chat continuity）这条最易回归的体验链路。改动 chat 渲染链必须保证 chat-history 端点不退化。
tags: [hipop, agent-os, smoke, chat-continuity]
---

# 体验评估（质量门 + chat 连续性）

## commit 前必跑的 smoke

```
bash tests/run_smoke.sh        # chat 端到端（需 server :8765）
make test                      # 自动聚合 tests/smoke_*.py（不含 chat）
make test-chat                 # chat 17 case
```

chat smoke 每 case 验 **4 件事**：HTTP 状态 / tool 是否被调 / 关键词命中 /
反 hallucinate 黑名单未命中。实测三家 LLM（Anthropic Haiku-4-5 / Qwen-Plus / DeepSeek-V3）全绿。

## chat 持久化

- 表 `chat_messages(... role, who, content, references_json, task_json, created_at)`。
- 写：`/api/chat` 存 user 最后一条 + agent 回复。PG 模式 INSERT 必须显式带 `tenant_id`（RLS WITH CHECK）。
- 读：`/api/chat-history/<store>` 默认 100 条；历史里的 task 前端 normalize 成「已完成」态。

## 切页面继承（chat continuity，最易回归）

切模块页 → chat_panel `init()` 重新 `Promise.all([GET /api/team, GET /api/chat-history])` 铺历史。
**关键失败模式**：`/api/chat-history` 返 500 → Promise.all reject → 整个 chat panel init 失败 →
表现为「切页面聊不见历史」（用户只见空白，无 traceback）。
**已踩坑**：PG 下 `created_at` 是 datetime，旧代码 inline slice 抛 TypeError；
统一走 `data._hhmm()` / `data._date10()` helper，**禁 inline slice**。

## 防回归红线

> 任何动 chat_messages 渲染链路的改动，必须保证 `smoke_chat.py:check_chat_history_endpoint`
> 200 且 `time` 字段为 `'HH:MM'`。覆盖度由 `tests/ws154_chat_coverage_gate.py` 把守。

回归：`tests/smoke_chat_persist.py` / `smoke_ws171_chat_history_pollution.py` /
`smoke_ws154_chat_coverage.py` / `smoke_judge.py`（graded 评估）。
