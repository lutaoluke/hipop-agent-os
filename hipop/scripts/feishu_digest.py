"""
飞书消息抽取 → LLM 提炼 → 写入 feishu_digest 表

逻辑:
  1. 列出当前用户/机器人能访问的群（im/v1/chats）
  2. 找包含 'hipop' / 'HIPOP' / '点购' 的群
  3. 拉最近 24h 消息（im/v1/messages）
  4. LLM 提炼: {who, category(操作/决策/反馈/其他), text(摘要)}
  5. 写入 feishu_digest

注意:
  - tenant_access_token 用 app_id / app_secret 获取（已有 server.feishu 复用）
  - 如果机器人不在群里, list 不到任何 chat → 写一条记录到 feishu_digest 标记
"""
import os, sys, json, sqlite3, time, datetime, requests

HERE = os.path.dirname(os.path.abspath(__file__))
HIPOP_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HIPOP_ROOT)
sys.path.insert(0, os.path.dirname(HIPOP_ROOT))

from server.feishu import _get_tenant_token

DB = os.environ.get("HIPOP_DB", "/Users/luke/code/hipop/hipop.db")


def list_chats(token: str):
    """返回机器人能访问到的所有群"""
    url = "https://open.feishu.cn/open-apis/im/v1/chats"
    chats = []
    page_token = None
    for _ in range(5):
        params = {"page_size": 100}
        if page_token: params["page_token"] = page_token
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=10).json()
        d = r.get("data", {})
        chats.extend(d.get("items", []))
        page_token = d.get("page_token")
        if not page_token: break
    return chats


def list_messages(token: str, chat_id: str, since_ts: int, limit: int = 50):
    """拉指定群最近 N 条消息（since_ts 以下）"""
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "page_size": limit,
        "sort_type": "ByCreateTimeDesc",
        "start_time": since_ts,
    }
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=10).json()
    return r.get("data", {}).get("items", []), r


def extract_text(msg):
    """从消息体提取纯文本"""
    body = msg.get("body", {})
    content = body.get("content", "{}")
    try:
        c = json.loads(content)
        return c.get("text", "") or c.get("content", "")
    except Exception:
        return content[:200]


def llm_digest(messages):
    """让 LLM 把 messages 列表提炼为结构化 digest"""
    import anthropic
    if not messages:
        return []
    client = anthropic.Anthropic()

    items = [{"i": i, "from": m.get("sender", {}).get("id", "unknown")[:10], "time": m.get("create_time"), "text": (m.get("_text") or "")[:300]}
             for i, m in enumerate(messages) if m.get("_text")]

    if not items:
        return []

    prompt = f"""以下是飞书 hipop 运营群最近的群消息 (从最新到最早):

{json.dumps(items, ensure_ascii=False, indent=1)}

请把每条消息分类到: 操作 / 决策 / 反馈 / 其他, 并给一句话摘要 (40字内).
忽略广告/无意义消息(category 设为 "其他" 且 text 留空).

返回 JSON 数组, 严格按下面格式 (不要任何其他内容):
[
  {{"i": 0, "category": "操作|决策|反馈|其他", "who": "...", "text": "摘要"}}
]
"""
    msg = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    text = text.strip()
    # 提取 JSON
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        return json.loads(text[start:end+1])
    except Exception:
        return []


def main():
    token = _get_tenant_token()
    chats = list_chats(token)
    print(f"机器人能访问 {len(chats)} 个群:")
    target_chats = []
    for ch in chats:
        name = ch.get("name", "")
        if any(k in name.lower() for k in ("hipop", "点购")):
            target_chats.append(ch)
            print(f"  ✓ {ch['chat_id']}  {name}")
        else:
            print(f"  · {ch['chat_id']}  {name[:30]}")

    if not target_chats:
        # 没找到相关群 → 退化：拉所有群第一条
        target_chats = chats[:3]

    since_ts = int(time.time()) - 86400 * 7  # 最近 7 天
    all_msgs = []
    for ch in target_chats:
        items, raw = list_messages(token, ch["chat_id"], since_ts, limit=30)
        print(f"  {ch.get('name','')} 拉到 {len(items)} 条消息")
        for m in items:
            m["_text"] = extract_text(m)
            m["_chat"] = ch.get("name") or ch["chat_id"]
        all_msgs.extend([m for m in items if m.get("_text") and len(m["_text"].strip()) > 4])

    print(f"合计有效消息 {len(all_msgs)} 条")
    if not all_msgs:
        # 写一条 marker 让前端不显示空
        conn = sqlite3.connect(DB)
        conn.execute("""
            INSERT INTO feishu_digest (source_chat, source_msg_id, source_time, who, category, text, raw_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("(无消息)", "marker", datetime.datetime.now().isoformat(),
              "Agent", "其他", "机器人当前未加入任何 hipop 群（或 24h 内无消息）",
              "请把机器人 cli_a96a395aaafa5cb5 拉进 hipop 运营群 + 跟单群"))
        conn.commit()
        conn.close()
        return 0

    digests = llm_digest(all_msgs[:30])
    print(f"LLM 抽取到 {len(digests)} 条 digest")

    # 写入数据库
    conn = sqlite3.connect(DB)
    written = 0
    for d in digests:
        i = d.get("i")
        if i is None or i >= len(all_msgs): continue
        m = all_msgs[i]
        # 去重 by msg_id
        msg_id = m.get("message_id", "")
        existing = conn.execute("SELECT 1 FROM feishu_digest WHERE source_msg_id=?", (msg_id,)).fetchone()
        if existing: continue
        ts = m.get("create_time")
        try:
            ts = datetime.datetime.fromtimestamp(int(ts)/1000).isoformat() if ts else ""
        except Exception:
            ts = ""
        conn.execute("""
            INSERT INTO feishu_digest (source_chat, source_msg_id, source_time, who, category, text, raw_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            m.get("_chat", ""), msg_id, ts,
            d.get("who") or m.get("sender", {}).get("id", ""),
            d.get("category") or "其他",
            d.get("text") or "",
            (m.get("_text") or "")[:500],
        ))
        written += 1
    conn.commit()
    conn.close()
    print(f"写入 {written} 条 feishu_digest")
    return written


if __name__ == "__main__":
    sys.exit(main() or 0)
