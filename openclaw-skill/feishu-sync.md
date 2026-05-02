---
name: feishu-sync
display_name: 飞书同步（DB → Bitable）
version: 0.1.0
author: hipop
description: 把 hipop.db 里的 wf3_logistics_hub / wf6_logistics_alerts / wf6_replenishment_queue 同步到飞书多维表格的主表+4 张子表。支持全量、按 SKU、按指定子表的模式；幂等（按主键 upsert）。已集成进 wf3 和 wf6 的命令行入口（默认跑完自动同步）。
tags: [feishu, sync, hipop]
---

## 功能概述

把 hipop 数据中枢（hipop.db）的内容映射到飞书 Bitable 上：

| 来源（hipop.db） | 目标（Bitable） | 主键映射 |
|---|---|---|
| wf3_logistics_hub.in_transit_total_qty | 主表 ERP-SKU 行的「发货在途」 | ERP-SKU |
| wf3_logistics_hub.groups_json 展开 | 子表 2「在途批次明细」 | SKU+货单 |
| wf6_logistics_alerts | 子表 1「物流告警」 | 告警ID |
| wf6_replenishment_queue（按 SKU 聚合） | 子表 3「经营决策」（丢货必补部分）| SKU |
| wf6 蓝色告警（清关完成-需约仓） | 子表 4「约仓动作」 | 货单号 |

**幂等**：所有写入用 `upsert_by_field`，按主键查找记录；存在 → update，不存在 → insert。

**双向关联**：子表 2 和子表 3 写入时自动填入主表 SKU 行的 record_id（飞书的双向关联字段）。

---

## 使用方法

### 命令行

```bash
# 全量同步所有 SKU 到 4 张子表
python3 -m scripts.feishu_sync

# 只同步指定 SKU
python3 -m scripts.feishu_sync --sku TBJ0057A TBA0210A

# 只同步指定子表
python3 -m scripts.feishu_sync --tables alerts warehouse_appt

# 组合
python3 -m scripts.feishu_sync --sku TBJ0057A --tables hub alerts
```

### 自动集成

工作流三和工作流六的 main 默认跑完会自动调用 sync。如果不希望同步，加 `--no-sync`：

```bash
python3 workflows/wf_logistics_status.py TBJ0057A           # 跑完自动 sync hub
python3 workflows/wf_logistics_status.py TBJ0057A --no-sync # 不 sync
python3 workflows/wf_logistics_alerts.py                    # 跑完自动 sync alerts + warehouse_appt
python3 workflows/wf_logistics_alerts.py --resolve 12 --status 已确认丢货  # 反馈后自动 sync 全部
```

### Python 调用

```python
from scripts.feishu_sync import sync_all

# 全量
sync_all()

# 指定 SKU
sync_all(skus=["TBJ0057A"])

# 指定表
sync_all(tables=["alerts", "warehouse_appt"])
```

---

## 子表 → 飞书字段映射

### 子表 1 物流告警

| Bitable 字段 | hipop.db 来源 | 备注 |
|---|---|---|
| 告警ID | wf6.alert_id | 主字段 |
| 货单号 | wf6.order_no | |
| 物流公司 | wf6.forwarder | 单选 |
| 告警原因 | wf6.alert_reason | 单选（7 种 + 取严合并）|
| 告警级别 | wf6.alert_level | 单选 红/橙/黄/蓝 |
| 阶段 | wf6.stage | 单选 |
| 涉及SKU | wf6.sku_list_json 解析 | 字符串 "TBJ0057A×8, …" |
| 停留天数 | wf6.actual_stay_days | 数字 |
| 历史均值 | wf6.history_stage_days | 数字 |
| 阈值 | wf6.threshold_days | 数字 |
| 超出 | wf6.excess_over_threshold | 数字 |
| 主责 / 协同 | wf6.action_owner / supervisor | 单选 |
| 需要的动作 / 需回填 | wf6.required_action / feedback_fields | 文本 |
| 操作状态 | wf6.ops_status | 单选 |
| 物流回复 | wf6.ops_contact_log 最新一条 | 文本 |
| 是否丢货 | 推断自 ops_status | 单选 |

### 子表 2 在途批次明细

| Bitable 字段 | hipop.db 来源 |
|---|---|
| SKU+货单 | "{sku} · {order_no}" |
| SKU / 货单号 / 件数 / 国家 / 平台 / 物流公司 | 同名直取 |
| 当前阶段 / 当前状态原文 / 阶段停留天数 / 历史阶段耗时 / 是否卡单 | 同名直取 |
| 关联主表-SKU | 主表 ERP-SKU 行的 record_id |

### 子表 3 经营决策

| Bitable 字段 | 来源 |
|---|---|
| SKU | wf6_replenishment_queue.sku |
| 丢货必补 / 本周必补总量 | SUM(lost_qty) |
| 触发原因 | ["丢货补货"] |
| 周标签 | week_tag |
| 操作状态 | "未处理" |
| 关联主表-SKU | 主表 record_id |

> wf5 重构后，趋势/日均/预测/wf5 建议补货 等字段会一并写入。

### 子表 4 约仓动作

| Bitable 字段 | 来源 |
|---|---|
| 货单号 | wf6 alert.order_no（reason='清关完成-需约仓'） |
| SKU列表 | 解析 sku_list_json |
| 状态 | "待约仓" / "已约仓"（看 wf6.ops_status） |
| 责任人 | "运营" |

---

## 注意事项

- 主表 765 SKU 全量同步约 30-60 秒（每 SKU 多次 API 调用，限速）
- Bitable 字段类型很多是文本（导入时识别），数字字段先用字符串写入
- token 30 天内自动 refresh，过期会触发刷新
- 出错重试一次后抛异常（feishu_bridge.py 已实现）

---

## 执行指令

1. 解析参数（--sku / --tables）
2. 调用 sync_all(skus, tables, verbose=True)
3. 默认全量 + 全表
4. 集成进 wf3/wf6 时自动调用，跳过用 --no-sync
