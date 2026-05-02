---
name: coordinator
display_name: 点购 Agent 协调器（意图路由）
version: 0.1.0
author: hipop
description: 接受自然语言指令，识别意图并路由到对应工作流。支持查 SKU/查货单/反馈状态/店铺概览/周报触发 5 类。基于规则匹配（不依赖 LLM），可独立 CLI 跑或被钉钉/飞书机器人 wrap。结果可选推送到群里。
tags: [coordinator, hipop, agent]
---

## 功能概述

把运营/刘鹤/Luke 的自然语言指令翻译成对应工作流调用，并组装结果返回。

**5 类意图**：

| 意图 | 触发示例 | 路由 |
|---|---|---|
| query_sku | "查 TBJ0057A" / "看 TBJ0057A,TBA0210A" | 读 hub + 列在途 / 卡单 / 待运营 / 关联告警 |
| query_order | "PDZ0027158 怎么样" / "看下 PDZ0026697" | 列该货单的所有 alerts + 涉及 SKU |
| update_status | "PDZ0027158 已确认丢货 备注:战争丢失" | update_alert_status + 触发 sync |
| scope_overview | "看 noon UAE" / "看 KSA" | 按 country/platform 过滤 hub，统计在途/卡单 |
| weekly_report | "周报" / "全量跑" | 提示用 CLI 跑全量（避免 IM 触发长任务）|

---

## 意图识别规则

### SKU 模式
正则 `\b([A-Z]{2,4}\d{4,6}[A-Z]?)\b`，例：`TBJ0057A` `SDA1874A`

### 货单号模式
正则 `\b(PD[A-Z]\d{7})\b`，例：`PDZ0027158` `PDW0026965`

### 状态关键字
| ops_status | 触发关键字 |
|---|---|
| 已确认推进 | 确认推进 / 已推进 / 继续推进 / 确认在途 |
| 已确认丢货 | 确认丢货 / 已丢货 / 确认丢失 / 丢了 / 丢货 |
| 已约仓     | 已约仓 / 约仓完成 / 约好了 |
| 已结案     | 结案 / 关闭 / 已完成 |
| 处理中     | 处理中 / 在处理 / 正在处理 |

### 备注提取
正则匹配 `备注:` `理由:` `原因:` 后的内容。

### 国家/平台
`KSA` `UAE` `noon` `amazon`

---

## 使用方法

### CLI

```bash
# 查 SKU
python3 -m scripts.coordinator "查 TBJ0057A"
python3 -m scripts.coordinator "查 TBJ0057A TBA0210A"

# 查货单
python3 -m scripts.coordinator "看下 PDZ0027158"
python3 -m scripts.coordinator "PDZ0026697 怎么样"

# 反馈
python3 -m scripts.coordinator "PDZ0027158 已确认丢货 备注:战争丢失"
python3 -m scripts.coordinator "PDW0026965 已约仓 备注:5月3日"

# 店铺概览
python3 -m scripts.coordinator "看 noon UAE"
python3 -m scripts.coordinator "看 KSA"

# 周报
python3 -m scripts.coordinator "周报"

# 输出同时推送到飞书群（卡片）
python3 -m scripts.coordinator "查 TBJ0057A" --push
```

### Python 调用

```python
from scripts.coordinator import route, parse_intent

# 仅识别意图
intent = parse_intent("PDZ0027158 已确认丢货 备注:战争失踪")
# → {"intent": "update_status", "params": {"order_no": "PDZ0027158", "status": "已确认丢货", "note": "战争失踪"}}

# 识别 + 执行 + 返回 markdown 字符串
out = route("查 TBJ0057A")
print(out)

# 识别 + 执行 + 推送飞书卡片
route("查 TBJ0057A", push_card=True)
```

---

## 输出示例

### 查 SKU
```
**查询 SKU：TBA0210A, TBJ0057A**

— **TBA0210A**: 在途 64 件 / 7 批  ⚠️ 卡单
  • [红] PDZ0026970 海运超时+频繁推迟 (待处理)
  • [蓝] PDW0026965 清关完成-需约仓 (待处理)

— **TBJ0057A**: 在途 32 件 / 4 批  ⚠️ 卡单 🔔 待运营
  • [红] PDZ0027158 海运超时 (待处理)
```

### 查货单
```
**货单 PDZ0026697 的告警历史**

• alert#5 [红] 海运超时+频繁推迟
  停留 65.0天 / 历史均 27.6天 | 状态 待处理
  涉及: TBJ0056A×8, TBP0289A×4
```

### 店铺概览
```
**noon UAE 概览**

- 有在途的 SKU: 6 个
- 在途总件数: 75
- 卡单 SKU: 5 个 — ['TBA0210A', 'TBJ0056A', 'TBJ0057A', 'TBP0260A', 'TBP0289A']
```

---

## 集成方式

### 与钉钉/飞书 IM 接收（未来）

机器人接收消息 → 提取 message text → 调用 `route(text, push_card=False)` → 把返回值发回群里。

实现伪代码：
```python
@bot.on_message
def handle(msg):
    text = msg.content
    reply = route(text)
    bot.reply(reply)
```

### 与定时任务

cron 配置：
```cron
0 9 * * MON  cd /path/to/hipop && python3 -m scripts.coordinator "周报" --push
*/5 * * * *  cd /path/to/hipop && python3 -m scripts.feishu_pull
```

---

## 限制 + 后续

- ⚠️ 当前是规则匹配，复杂表达可能识别不到（fallback 到 unknown）
- ⚠️ "周报"目前只是返回提示，不直接跑全量（避免 IM 触发 5+ 分钟长任务，应该走 cron）
- 🚧 store_keyword 过滤暂用 country/platform 单层；多店铺隔离（运营 A 默认看 KSA）需要补 user_scope 表 + IM user_id → scope 映射

---

## 执行指令

1. 解析参数（位置参数为指令文本，--push 为是否推送到群）
2. 调用 route(text, push_card)
3. 标准输出打印结果（markdown 形式）
4. 如 --push，同时推卡片到飞书群
