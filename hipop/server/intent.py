"""
意图识别：用 Claude API 把自然语言解析成 skill + 参数
"""
import os
import json
import anthropic

client = anthropic.Anthropic()  # 自动读取 ANTHROPIC_API_KEY 环境变量

SYSTEM_PROMPT = """你是 HIPOP 跨境电商运营系统的意图识别模块。
根据用户消息，判断要执行哪个工作流（skill），并提取参数。

可用 skill：
- wf0_logistics：在途库存 & 物流预估。触发词：在途、到货、物流、更新在途、几天到。
  参数 skus：SKU 列表（如 ["TBJ0057A", "TBA0210A"]），不指定则为空列表（全量扫描）。
- wf3_sales：销售周期分析。触发词：销售、周期、月均、库存风险、预警。无参数。
- wf4_restock：补货建议。触发词：补货、建议补、要补多少。无参数。
- unknown：无法识别，需要澄清。

只返回 JSON，不要其他文字：
{"skill": "wf0_logistics", "skus": ["TBJ0057A"], "reason": "用户询问指定SKU到货时间"}
{"skill": "wf3_sales", "skus": [], "reason": "用户要看销售周期分析"}
{"skill": "unknown", "skus": [], "reason": "无法识别意图，请澄清"}
"""

def parse_intent(user_message: str) -> dict:
    """
    输入用户消息，返回 {"skill": str, "skus": list, "reason": str}
    """
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = msg.content[0].text.strip()
        # 提取 JSON（防止模型多输出了内容）
        start = text.find("{")
        end   = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception as e:
        return {"skill": "unknown", "skus": [], "reason": f"意图解析失败: {e}"}
