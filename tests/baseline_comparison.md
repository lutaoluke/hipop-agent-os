# WS-163: DeepSeek vs Opus Graded-Eval Baseline Comparison

## What this is

Decision① boundary study: run the **same chat smoke suite** twice — Arm A = production
model (**DeepSeek**), Arm B = strong-model baseline (**Opus**, `claude-opus-4-8`) — grade
every case with the deterministic 4-dim rubric (`smoke_chat.grade_case`, no LLM self-rating),
and look at the per-case gap to answer: *which cases are already mitigated by deterministic
routing/data-gates (→ keep DeepSeek), and which still depend on model strength (→ upgrade)?*

## How it was produced (reproducible, no fabrication)

- Runner: `tests/run_baseline_arm.py` (reuses smoke_chat's opener/fixtures/grader; retries
  transient HTTP 429/5xx with backoff so a rate-limited reply is **never graded as a model
  answer**; fail-closed aborts rather than write a contaminated matrix).
- Arm switch is pure env (no code change): `LLM_PROVIDER=anthropic ANTHROPIC_CHAT_MODEL=claude-opus-4-8`
  for Opus vs default `deepseek`.
- Coverage note (honest): the 25 LLM/deterministic cases of the original clean pair were
  measured 2026-06-10; the 6 later-added **T36** cases are deterministic routes (0.0–0.1s,
  zero LLM call → model-independent by construction) and were measured 2026-06-11 under each
  arm and merged in. Both arms therefore cover the **full current 31-case suite**. The
  ~8 LLM-engaged cases could not be re-measured on Opus on 2026-06-11 because the Opus
  backend shares the local Claude-Code OAuth subscription quota with the coding agent and
  hard-429'd; their genuine clean Opus measurements from the 2026-06-10 run are used as-is
  (no estimation, no placeholder).
- "50-case (HIPOP-DG-50)" in the issue is E9.2's future browser-replay suite; the current
  real suite is `smoke_chat.CASES` = 31. Full coverage here = 31.

## Summary

- Cases analyzed: **31** (full current suite, both arms)
- Average overall gap: **0.015**
- Keep DeepSeek (gap ≤ 0.05): **29 cases (94%)**
- Investigate (0.05–0.15): **0 cases**
- Upgrade signal (gap > 0.15): **2 cases (6%)**

## Difference Matrix

| Case | DeepSeek | Opus | Gap | Category |
|------|---------|------|-----|----------|
| 数据更新时间问答（不能假说今天） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 商品总数（动态 product 2884 / SKU 3662） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 商品总数 + 上架未上架细分（动态 product/SKU 维度） | 1.000 | 1.000 | 0.000 | ✓ keep |
| WS-148 近30天销量 TopN（list_products 确定性路由 + 证据） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 店铺整体（动态在售 SKU 2092 + 红色告警） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 红色告警（要真数 2） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 补货建议（数据新鲜走 compute_replenishment；陈旧走上传引导） | 0.975 | 0.975 | 0.000 | ✓ keep |
| 单 SKU 查询 TBJ0059A（必含 SKU 名 + 不能编不存在字段） | 0.750 | 0.975 | 0.225 | ✗ upgrade |
| T04 TBB0116A 30d 口径（动态 tool_query_sku 口径） | 1.000 | 1.000 | 0.000 | ✓ keep |
| T04 快照过期/缺失边界（动态：STALE_TST001 当前不存在时必须诚实未找到） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 导出表格（必走 export_table，必含真实 /api/download xlsx 链接） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 打开页面（必走 navigate_user_to，不能编虚构域名） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 发飞书（必须诚实告知不能主动推） | 0.975 | 0.975 | 0.000 | ✓ keep |
| 用户拒绝刷新（要警示陈旧 + 给答案） | 0.750 | 0.750 | 0.000 | ✓ keep |
| 数据新鲜度精确度（不能编精确时间戳） | 0.975 | 0.975 | 0.000 | ✓ keep |
| 刷新库存（必走 run_workflow，禁编侧边栏路径） | 1.000 | 1.000 | 0.000 | ✓ keep |
| T36: 刷新 ERP 商品库和销量价格（task_id 必须可回读） | 0.875 | 0.875 | 0.000 | ✓ keep |
| T36 负控：询问 ERP 商品库和销量价格上次刷新时间（不得创建 wf2 任务） | 0.975 | 0.975 | 0.000 | ✓ keep |
| T36 负控4：无问号『上次什么时候刷新过』只读时间（不得创建 wf2 任务） | 0.975 | 0.975 | 0.000 | ✓ keep |
| T36 负控5：无问号『多久前刷的』只读时间（不得创建 wf2 任务） | 0.975 | 0.975 | 0.000 | ✓ keep |
| T36 负控2：询问句『能不能帮我刷新 ERP 商品库和销量价格?』（结构门干净回复，零工具） | 0.975 | 0.975 | 0.000 | ✓ keep |
| T36 负控3：假设句『如果刷新 ERP 商品库和销量价格会怎样?』（只说明影响，零工具） | 0.975 | 0.975 | 0.000 | ✓ keep |
| 刷新物流（必走 v2，禁老 wf3 全局 env） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 扫 ERP 物流（用户口语，必走 run_workflow） | 1.000 | 1.000 | 0.000 | ✓ keep |
| 只查不触发（必不出现已触发字样） | 0.750 | 0.975 | 0.225 | ✗ upgrade |
| 改告警状态必须走 Plan（不能一步直接 update_alert_status） | 0.750 | 0.750 | 0.000 | ✓ keep |
| T07-1 销量 TopN freshness gate（不能模拟数 + workflow_task=null） | 0.975 | 0.975 | 0.000 | ✓ keep |
| T07-2 最畅销商品查询（freshness gate 不误拦否定场景） | 0.975 | 0.975 | 0.000 | ✓ keep |
| T26: 不存在货单号（必调 query_order_live，含未找到，禁假称正在查） | 1.000 | 1.000 | 0.000 | ✓ keep |
| WS-150: 飞书确定性拒绝（不能主动发飞书/通知群） | 0.975 | 0.975 | 0.000 | ✓ keep |
| WS-150: 飞书拒绝 - 推到群变体 | 0.975 | 0.975 | 0.000 | ✓ keep |

## Decision① boundary conclusion

**KEEP DeepSeek + keep investing in deterministic routing / data-gates.**

The average overall gap of 0.015 means the existing deterministic routing and data-freshness
gates have already flattened the model difference on 29/31 (94%) of cases — for those, Opus
buys nothing, so DeepSeek is sufficient. Only **2 cases (6%)** still depend on model strength:

- 单 SKU 查询 TBJ0059A: DeepSeek 0.75 vs Opus 0.975 (gap 0.225)
- 只查不触发: DeepSeek 0.75 vs Opus 0.975 (gap 0.225)

Both lose points on `correct_source` (DeepSeek occasionally answers the single-SKU / "query
only, don't trigger" turns without firing the expected tool). The boundary verdict: this is
cheaper to close by **strengthening the deterministic route/guard for those two intents**
than by upgrading the whole chat backend to Opus. Re-open the model-upgrade question only if
the upgrade-signal set grows past tolerance — which the CI gate now enforces (below).

## How this feeds the regression net (acceptance #4)

Graded scores are wired into CI as real thresholds, not just pass/fail:

- **Offline, required `make test` lane** — `tests/smoke_graded_decision.py` (auto-discovered):
  fails closed if any current smoke case is absent from a baseline (coverage), if avg gap
  exceeds 0.05 or keep-rate drops below 90% (decision① flips → "consider upgrade"), or if the
  committed DeepSeek averages regress below documented floors. Deterministic, no server.
- **Live `chat e2e` lane** — `tests/smoke_graded_threshold.py` (invoked by
  `tests/ci_chat_e2e_gate.sh` with `HIPOP_GRADED_REQUIRE_SERVER=1`): grades live responses and
  fails if any dimension regresses below `baseline − 0.07`. Fail-closed if the server is
  missing in the live lane (never a silent green).
- Fail-then-pass unit coverage of the gate logic: `tests/test_graded_eval.py`.
