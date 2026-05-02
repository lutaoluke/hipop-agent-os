"""
飞书通知工具 - 所有工作流共用
"""
import json
import requests

def load_config(company="hipop"):
    import os
    config_path = os.path.join(os.path.dirname(__file__), f"../config/{company}.json")
    with open(config_path) as f:
        return json.load(f)

def send_text(msg, company="hipop"):
    cfg = load_config(company)
    webhook = cfg["feishu"]["webhook"]
    requests.post(webhook, json={"msg_type": "text", "content": {"text": msg}})

def send_card(title, content, color="blue", company="hipop"):
    """发送卡片消息"""
    cfg = load_config(company)
    webhook = cfg["feishu"]["webhook"]
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color
            },
            "elements": [{
                "tag": "div",
                "text": {"tag": "lark_md", "content": content}
            }]
        }
    }
    resp = requests.post(webhook, json=payload)
    return resp.json()

def send_report(title, rows, company="hipop"):
    """发送表格式报告卡片"""
    cfg = load_config(company)
    webhook = cfg["feishu"]["webhook"]

    # 格式化为 markdown 表格
    if rows:
        header = "| " + " | ".join(str(k) for k in rows[0].keys()) + " |"
        sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
        body = "\n".join(
            "| " + " | ".join(str(v) for v in r.values()) + " |"
            for r in rows
        )
        table = f"{header}\n{sep}\n{body}"
    else:
        table = "（无数据）"

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue"
            },
            "elements": [{
                "tag": "div",
                "text": {"tag": "lark_md", "content": table}
            }]
        }
    }
    resp = requests.post(webhook, json=payload)
    return resp.json()

if __name__ == "__main__":
    send_card(
        "测试通知",
        "**notify.py** 工具加载成功 ✓",
        color="green"
    )
    print("发送成功")
