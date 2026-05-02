"""
HIPOP 工作台 - 集中存放 mock 数据
"""

TEAM_MEMBERS = [
    {"name": "Cherry", "role": "运营 · 主理", "online": True, "tasks": 3, "is_me": True, "avatar": "C"},
    {"name": "王小明", "role": "运营 · 助理", "online": True, "tasks": 2, "is_me": False, "avatar": "王"},
    {"name": "刘鹤", "role": "跟单 · 跨店", "online": True, "tasks": 4, "is_me": False, "avatar": "刘"},
    {"name": "张组长", "role": "小组长", "online": True, "tasks": 0, "is_me": False, "avatar": "张"},
    {"name": "李总监", "role": "总监", "online": False, "tasks": 0, "is_me": False, "avatar": "李"},
]

TRAFFIC_MOCK = {
    "uv_change": -18,
    "pv_change": -22,
    "uv_today": 1820,
    "pv_today": 5430,
    "ctr_change": -3.2,
    "top_skus_affected": ["TBJ0057A", "TBA0210A", "TBP0289A"],
    "as_of_date": "2026-05-02",
    "note": "(数据源 mock：noon 后台流量 API 接通后即用真数据)",
}

SELECTION_MOCK = [
    {
        "candidate": "新品 A · 折叠水杯",
        "verdict": "推荐",
        "score": 0.78,
        "reason": "类目相似品成功率 65%，目标人群与现有客群高重合度",
        "details": "类目: 户外用品 · 预估利润率 35% · 类目饱和度 中",
    },
    {
        "candidate": "新品 B · 户外风扇",
        "verdict": "中性",
        "score": 0.52,
        "reason": "季节性强（5-9 月旺季）, 预估利润率合格但风险中",
        "details": "类目: 小家电 · 预估利润率 22% · 类目饱和度 中高",
    },
    {
        "candidate": "新品 C · LED 钥匙扣",
        "verdict": "不推荐",
        "score": 0.21,
        "reason": "类目饱和度过高 + 同类品利润率仅 8%",
        "details": "类目: 数码配件 · 预估利润率 8% · 类目饱和度 高",
    },
]

WORK_LOG_MOCK_FEISHU = [
    {"time": "10:12", "who": "刘鹤", "text": "飞书群确认 PDZ0027158 丢货 (战争失踪)", "ref": "feishu/mock", "tag": "飞书"},
    {"time": "11:08", "who": "王小明", "text": "采纳 TBA0210A 调价 +12% 建议", "ref": "feishu/mock", "tag": "飞书"},
    {"time": "13:24", "who": "运营群", "text": "noon 销量 CSV (KSA) 已上传，agent 正在解析", "ref": "feishu/mock", "tag": "飞书"},
]

PROGRESS_MOCK = {
    "task_id": "demo-001",
    "label": "noon 销量 CSV (KSA) 解析中",
    "current_step": 3,
    "total_steps": 5,
    "steps": [
        {"name": "上传 4 个文件", "status": "done"},
        {"name": "校验格式", "status": "done"},
        {"name": "解析 688 行订单", "status": "now"},
        {"name": "入库 wf2", "status": "pending"},
        {"name": "重跑 wf3 + wf5 决策", "status": "pending"},
    ],
}

CHAT_HISTORY_MOCK = [
    {
        "who": "Agent",
        "role": "agent",
        "time": "09:15",
        "content": "早上好 Cherry。今日发现 7 个 SKU 销量急速下降, 4 个货单卡单。点查看建议 →",
        "tag": "主动",
    },
    {
        "who": "刘鹤",
        "role": "user",
        "time": "10:08",
        "content": "PDZ0027158 战争丢失了, 下周再发一批",
        "tag": "",
    },
    {
        "who": "Agent",
        "role": "agent",
        "time": "10:09",
        "content": "已记录 PDZ0027158 → 已确认丢货。已自动同步到 wf6_logistics_alerts + 飞书。📎 wf6",
        "tag": "执行",
    },
    {
        "who": "Cherry",
        "role": "user",
        "time": "11:06",
        "content": "TBA0210A 这周怎么样? 要不要补货?",
        "tag": "",
    },
    {
        "who": "Agent",
        "role": "agent",
        "time": "11:06",
        "content": "TBA0210A KSA 加速增长, 7 天销量 84 件 (+38%), 库存可撑 9.2 天。建议补货 200 件。📎 wf2 / wf5",
        "tag": "建议",
    },
]

QUICK_COMMANDS = [
    "上传 noon CSV",
    "查 SKU 健康",
    "看 KSA 卡单",
    "本周补货建议",
    "选品候选评估",
]

MARKETING_MOCK = {
    "this_week": [],
    "next_week": [
        {"name": "noon 平台 May Day Promo", "start": "2026-05-08", "skus": ["TBJ0057A", "TBA0210A"]},
    ],
    "note": "(数据源 mock)",
}
