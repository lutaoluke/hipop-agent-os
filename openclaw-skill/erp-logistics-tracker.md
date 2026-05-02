---
name: erp-logistics-tracker
display_name: 在途商品与到货时间计算
version: 0.3.0
author: hipop
description: 跨境电商在途库存分析工具。对接 dbuyerp ERP + 主流货代物流追踪网站，两阶段扫描自动过滤有在途订单的 SKU，计算在途数量、按货代加权物流均值、超期自动修正、最快到货批次。结果可写回飞书多维表格。
tags: [ecommerce, logistics, erp, feishu, cross-border]
---

## 功能概述

针对使用 **dbuyerp** 的跨境卖家，自动完成：

1. **全量扫描**：从 ERP 快速过滤出所有有在途订单的 SKU（第一阶段，纯 API，无浏览器）
2. **在途库存**：逐单查询有在途 SKU 的发货数量明细（第二阶段）
3. **物流时长均值**：跨 SKU 扫描近期已完成货单，**按货代分组**加权平均（最新在前，权重 0.5/0.3/0.2）
4. **超期自动修正**：若在途批次已过天数 > 该货代历史均值，用 `已过天数 + 阶段估算剩余` 作为实测总时长，回推上调货代均值；修正结果写回模块缓存，同进程内后续 SKU 自动受益
5. **最快到货批次**：结合修正后均值和已过天数，推算最快一批到货时间和数量
6. **剩余批次估算**：对超期批次用物流节点阶段历史数据（清关中 / 海运中等）估算剩余天数
7. **写回飞书**（可选）：自动更新多维表格中的在途相关字段

支持物流商：**义特无忧**（nextsls）/ **安时达** / **飞坦**（fleetan）

---

## 安装前准备

### 1. 安装依赖

```bash
pip install requests playwright
playwright install chromium
```

### 2. 创建配置文件

在你的项目目录下新建 `config/hipop.json`（或 `erp_logistics_config.json`）：

```json
{
  "erp": {
    "url": "https://www.dbuyerp.com",
    "username": "你的ERP账号",
    "password": "你的ERP密码"
  },
  "platform": {
    "name": "Noon KSA",
    "store_keyword": "KSA"
  },
  "db": {
    "path": "./hipop.db",
    "table": "sa_main",
    "sku_column": "ERP-SKU"
  },
  "feishu": {
    "app_id": "cli_xxxxxxxxxxxxxxxx",
    "app_secret": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "base_id": "多维表格的Base ID",
    "table_id": "数据表ID",
    "token_file": "./feishu_token.json"
  },
  "logistics_tracking": {
    "义特无忧KSA": "http://tracking.nextsls.com/trace?app=61a0c12f69c1d654e75a49e6",
    "安时达KSA":   "https://logistics.ontaskksad2d.com/#/home",
    "安时达UAE":   "https://logistics.ontaskksad2d.com/#/home",
    "飞坦":        "https://tracking.fleetan.com/#/"
  }
}
```

### 3. 飞书授权（写回功能需要）

首次使用前运行一次，获取 user_access_token：

```bash
python3 feishu_auth.py
```

### 4. 多维表格字段要求

| 字段名 | 类型 | 说明 |
|--------|------|------|
| ERP-SKU | 文本 | SKU 编号，查找记录用（必须） |
| 发货在途 | 数字 | 在途总件数 |
| 到货周期 | 文本 | 如"75天 ≈ 2.5个月（近3笔，含超期修正）" |
| 最近一批到货数量 | 数字 | 最快批次件数 |
| 最新一批货到货预估时间 | 文本 | 如"2026-05-09（还需~19天）" |

---

## 使用方法

```
/erp-logistics-tracker [SKU列表]
```

| 调用方式 | 效果 |
|----------|------|
| `/erp-logistics-tracker` | 全量模式：扫描数据库所有 SKU，自动过滤有在途的分析 |
| `/erp-logistics-tracker TBJ0057A` | 指定模式：只分析该 SKU |
| `/erp-logistics-tracker TBJ0057A TBA0210A TBC0168A` | 指定多个 SKU |

---

## 输出示例

```
第一阶段：快速扫描 765 个 SKU 的 ERP 在途状态...
  [312/765] TBJ0057A ✓ 有在途 4 批
扫描完成：23/765 个 SKU 有在途库存

第二阶段：对 23 个在途 SKU 做物流追踪分析...

══════════════════════════════════════════
  SKU: TBJ0057A
══════════════════════════════════════════
① 在途库存：32件（4批）
   PDZ0028823 | 待签收 | 安时达UAE   | 2026-04-09 | 8件
   PDW0028803 | 待签收 | 义特无忧KSA | 2026-04-08 | 8件
   PDW0027331 | 待签收 | 义特无忧KSA | 2026-02-08 | 8件
   PDZ0027158 | 待签收 | 安时达UAE   | 2026-02-06 | 8件

② 平均物流时长：75天（近3笔加权，含超期修正）
   安时达UAE：92天  义特无忧KSA：101天

③ 最快到货：8件，预计 2026-05-09（还需约19天）
   当前节点：Customs declaration / 目的港清关中

④ 剩余批次：
   PDW0027331 | 8件 | 还需~30天 | 2026-05-20
   PDZ0028823 | 8件 | 还需~81天 | 2026-07-10
   PDW0028803 | 8件 | 还需~90天 | 2026-07-19

→ 超期修正：安时达UAE 54→92天  义特无忧KSA 60→101天
```

---

## 店铺过滤（store_keyword）

ERP 中同一账号可能管理多个渠道店铺（如 noon KSA、noon UAE、Amazon 等）。`store_keyword` 确保物流计算只使用**目标平台**的发货单，避免不同渠道的物流数据相互污染。

| 配置项 | 位置 | 说明 |
|--------|------|------|
| `platform.store_keyword` | `config/hipop.json` | 目标店铺名称关键字，如 `"KSA"` |

**过滤生效范围：**

- **物流均值计算**（`get_forwarder_avg_days`）：扫描近30页已完成货单时，只统计店铺名含 `store_keyword` 的订单，确保各货代均值基于本渠道真实数据
- **在途库存扫描**（`get_all_orders`）：拉取指定 SKU 的发货单时，同样过滤掉非目标店铺的订单

**规则：**
- 大小写不敏感（`"KSA"` 可匹配 `"Noon KSA"`、`"noon-ksa"` 等）
- 若 `store_keyword` 为空或未配置，则不过滤（保留全部订单）
- 当前 hipop noon KSA 配置值为 `"KSA"`

---

## 物流均值计算说明

| 步骤 | 说明 |
|------|------|
| 跨 SKU 扫描 | 扫描近30页已完成货单，按货代分组，取最新5笔加权（0.5/0.3/0.2） |
| 超期检测 | 若在途批次 `已过天数 > 该货代均值`，视为超期 |
| 超期修正 | 超期批次的 `已过天数 + 阶段估算剩余` 作为实测总时长，取 max(历史均值, 实测均值) 上调 |
| 缓存回写 | 修正后的货代均值写回模块缓存，同进程内后续 SKU 自动使用修正值 |
| 批次回写 | 非超期批次的 remaining_days 也用修正后货代均值重新计算 |

---

## 注意事项

- 全量扫描第一阶段仅调 ERP API，765 个 SKU 约需5分钟，无浏览器开销
- 第二阶段 Playwright 查物流网站，每个批次3~8秒
- 单独运行某 SKU 时，超期修正仅基于该 SKU 自身在途数据；全量或多 SKU 运行时，修正结果跨 SKU 传导
- 义特无忧、安时达、飞坦以外的货代显示"暂不支持"，不影响其他字段计算
- 飞书写回使用 user_access_token，有效期2小时，refresh_token 有效期30天

---

## 执行指令

1. 确认当前目录下存在配置文件，若不存在引导用户完成安装前准备
2. 解析 `$ARGUMENTS`：有参数则指定模式，无参数则全量模式（从数据库读取所有 SKU）
3. 全量模式先做第一阶段快速扫描，过滤出有在途 SKU 后再进入第二阶段
4. 在同一 Python 进程内顺序处理，超期修正结果自动传导
5. 逐 SKU 输出四个字段分析结果
6. 若配置了飞书信息，自动写回多维表格；最后汇总展示所有有在途 SKU 的结果表格
