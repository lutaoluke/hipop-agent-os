"""
HIPOP Skill Server
- POST /feishu/webhook  飞书事件回调（消息接收）
- GET  /health          健康检查
"""
import asyncio
import json
import os
import sys
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server.feishu import reply_text, send_card, send_text
from server.intent import parse_intent
from server.skills import dispatch

app = FastAPI(title="HIPOP Skill Server + Agent OS")

# ── Phase 1: 工作台 UI + JSON API ─────────────────────────
_SERVER_DIR = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(_SERVER_DIR, "static")), name="static")

from server.pages import router as _pages_router
from server.api import router as _api_router
app.include_router(_pages_router)
app.include_router(_api_router, prefix="/api")

# 防重放：记录已处理的 message_id
_processed = set()

# ── 健康检查 ─────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}

# ── 飞书 Webhook ──────────────────────────────────────────
@app.post("/feishu/webhook")
async def feishu_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    # 飞书 URL 验证握手
    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge")})

    header = body.get("header", {})
    event  = body.get("event", {})

    # 只处理消息事件
    if header.get("event_type") != "im.message.receive_v1":
        return JSONResponse({"code": 0})

    message    = event.get("message", {})
    message_id = message.get("message_id", "")
    chat_id    = message.get("chat_id", "")
    msg_type   = message.get("message_type", "")

    # 防止重复处理
    if message_id in _processed:
        return JSONResponse({"code": 0})
    _processed.add(message_id)

    # 只处理文本消息
    if msg_type != "text":
        return JSONResponse({"code": 0})

    try:
        content = json.loads(message.get("content", "{}"))
        text    = content.get("text", "").strip()
    except Exception:
        return JSONResponse({"code": 0})

    # 去掉 @ 机器人的前缀（飞书会在 text 里带上 @用户名）
    import re
    text = re.sub(r"@\S+\s*", "", text).strip()
    if not text:
        return JSONResponse({"code": 0})

    # 异步执行，立即返回 200（飞书要求3秒内响应）
    background_tasks.add_task(handle_message, chat_id, message_id, text)
    return JSONResponse({"code": 0})


async def handle_message(chat_id: str, message_id: str, text: str):
    """后台处理：识别意图 → 执行 skill → 回复结果"""
    # 先回复"收到，正在处理"
    reply_text(message_id, f"⏳ 收到：「{text}」\n正在识别并执行，请稍候...")

    # 意图识别
    intent = parse_intent(text)
    skill  = intent.get("skill", "unknown")
    skus   = intent.get("skus", [])

    if skill == "unknown":
        reply_text(message_id,
            f"🤔 未能识别指令：{intent.get('reason', '')}\n\n"
            f"可用指令示例：\n"
            f"• 更新所有 SKU 在途库存\n"
            f"• 查一下 TBJ0057A 到货时间\n"
            f"• 跑一遍销售周期分析\n"
            f"• 给我补货建议")
        return

    # 执行 skill（在线程池里跑，不阻塞事件循环）
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, dispatch, skill, skus)

    # 截取结果（飞书消息有长度限制）
    summary = result[:2000] if len(result) > 2000 else result

    skill_names = {
        "wf0_logistics": "在途库存 & 物流预估",
        "wf3_sales":     "销售周期分析",
        "wf4_restock":   "补货建议",
    }
    title = f"✅ {skill_names.get(skill, skill)} 执行完成"
    send_card(chat_id, title, f"```\n{summary}\n```")
