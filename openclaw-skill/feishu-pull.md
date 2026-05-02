---
name: feishu-pull
display_name: 飞书反向同步（Bitable → DB）
version: 0.1.0
author: hipop
description: 轮询飞书子表里运营/刘鹤更新的字段（操作状态 / 物流回复 / 约仓状态），回写到 hipop.db 的 wf6_logistics_alerts。终态时联动 wf6_replenishment_queue。和 feishu-sync 配合形成完整闭环，幂等无循环。
tags: [feishu, sync, hipop, ops]
---

## 功能概述

`feishu-sync` 是 DB → Bitable 单向写出，`feishu-pull` 是反向闭环：把运营/刘鹤在飞书表里改的字段拉回 DB。

**回写映射**：

| 飞书字段（子表） | hipop.db 字段 | 处理 |
|---|---|---|
| 子表 1.操作状态 | wf6_logistics_alerts.ops_status | 通过 `update_alert_status` 更新；终态自动写 resolved_at；"已确认丢货" 联动写 wf6_replenishment_queue |
| 子表 1.物流回复 | wf6_logistics_alerts.ops_contact_log | 追加（避免重复）|
| 子表 4.状态 | 对应 wf6 alert (alert_reason='清关完成-需约仓') 的 ops_status | "已约仓" → "已约仓"；"已入仓" → "已结案" |

**幂等 + 无循环**：
- 飞书值 == DB 值时跳过，不触发更新
- 即使 sync_to_feishu 把同样的值再推到飞书，下次 pull 时还是一致 → 不动 → 稳定

---

## 使用方法

### 一次性拉取

```bash
python3 -m scripts.feishu_pull
```

输出示例：
```
→ 拉取物流告警变更
  ⇄ alert#3: 待处理 → 已约仓  +log(5月3日已约仓…)
   1 条变更回写
→ 拉取约仓动作变更
  ⇄ PDW0026965 约仓: 已约仓 → 已结案
   1 条变更回写
```

### 持续轮询

```bash
python3 -m scripts.feishu_pull --watch                   # 每 5 分钟拉一次
python3 -m scripts.feishu_pull --watch --interval 60     # 每 60 秒一次
```

适合后台 daemon 模式。要做 systemd 服务的话直接 wrap 这个命令即可。

### Python 调用

```python
from scripts.feishu_pull import pull_all
n = pull_all()
```

---

## 闭环工作流

```
[运营/刘鹤]
   ↓ 在飞书子表改"操作状态"=已确认丢货, 物流回复="物流确认丢失"
[Bitable]
   ↓ feishu_pull --watch 轮询发现变更
[hipop.db wf6_logistics_alerts]
   ↓ update_alert_status() 写入新 ops_status + 追加 contact_log + 写 resolved_at
[hipop.db wf6_replenishment_queue]
   ↓ 自动联动写入 (因为是"已确认丢货"终态)
[下次 sync_to_feishu]
   ↓ 把 wf6_replenishment_queue 的丢货必补 → 子表 3 经营决策
[运营在子表 3 看到]
   "本周必补 X 件"
```

---

## 注意事项

- **不会循环**：DB 写一次后值不变；下次 pull 飞书读到同样的值不再触发
- **多人同时改**：飞书是 last-write-wins；pull 拿的是当前值，不冲突
- **物流回复追加**：如果用户新写了一条回复，会作为新 log entry 追加到 ops_contact_log，飞书"物流回复"字段下次同步显示最新一条
- **未识别的状态**：如果飞书操作状态是自定义新值（不在 7 选项里），update_alert_status 会按字面值写入；若不在 TERMINAL_STATUSES 里则不写 resolved_at

---

## 执行指令

1. 解析参数（`--watch` 是否轮询）
2. 单次：调用 pull_all() 一次后退出
3. 轮询：每 interval 秒调用一次，无变更也打印 "· 无变更"
4. 失败不退出：打印错误继续下一轮
