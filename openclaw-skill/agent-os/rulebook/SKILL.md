---
name: agent-os-rulebook
display_name: Agent OS · 规则书
version: 0.1.0
author: hipop
description: chat Agent 的行为规则书——SYSTEM_PROMPT 的调用规则、行为四象限决策流、以及反 hallucinate 三层防护（Prompt 硬约束 + 门控 stub tool + _safety.py 后处理）。这些是确定性规则，确定性部分应进 verifier，不应往 agent.py SYSTEM_PROMPT 里堆。本 skill 是规则总览与索引。
tags: [hipop, agent-os, rules, anti-hallucinate]
---

# 规则书（chat Agent 行为总纲）

## 调用规则（SYSTEM_PROMPT）

- **业务数据必须先调 tool 拿真数据再答**，不凭记忆。
- `update_alert_status` 等**写入**类，确认用户意图后再调；`run_workflow` 不需二次确认。
- 用户报告状态变化（"已到货""已处理"）→ **必须重调 tool 验证**，不轻信。
- 禁编 URL / 禁宣称未做的事；时间戳只到**日期**粒度；表格列限**真实字段**。

## 行为四象限（决策流）

识别意图 → 查依赖源新鲜度 → 落「直接答 / 触发刷新 / 引导上传 / 坚持旧数据」。
口径详表归 [`fact-source-contract`](../fact-source-contract/SKILL.md)，本书只钉「先查源再答」这条铁律。

## 反 hallucinate 三层防护（必须配套部署）

1. **Prompt 硬约束**：`agent.py:SYSTEM_PROMPT` 6 条强制规则（即上面调用规则）。
2. **门控 stub tool**：`export_table` / `navigate_user_to` / `notify_via_feishu`，劫持
   "导出 / 打开页面 / 发飞书" 三个最常 hallucinate 的触发点（详见 [`governance-gate`](../governance-gate/SKILL.md)）。
3. **`_safety.py` 后处理**：扫 reply 里未授权域名 / 精确时间戳 / wf5 不存在字段 / 假宣称 →
   命中加 banner + 写 `hallucination_warnings` 透回前端。

## 边界：规则进 verifier，不进 prompt

> 确定性规则应落成 verifier / 门控 tool / 后处理，**不应往 `agent.py:SYSTEM_PROMPT` 里堆**
> （CODEOWNERS 锁定 agent.py）。新增确定性约束走代码 + smoke，不靠加 prompt 句子。

回归：`tests/smoke_safety.py` / `smoke_ws133_no_fabrication_gate.py` /
`smoke_fake_action_gate.py` / `smoke_fake_task_id_gate.py` / `smoke_agent_antiregress_ratchet.py`。
