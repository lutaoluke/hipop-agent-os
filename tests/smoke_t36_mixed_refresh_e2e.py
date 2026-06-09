"""Smoke: WS-97 T36-S3 round-8 mixed refresh E2E.

This smoke drives the real API path:
  /api/chat -> agent.chat -> _provider_openai.run -> agent._exec_tool(run_workflow)
  -> _safety.sanitize_reply

The OpenAI-compatible client is faked so the test is deterministic and does not
need live LLM credentials. The tool boundary is patched to avoid launching real
ERP jobs while still exercising the provider function-call loop.

Fail-then-pass:
  Before round-8, sanitize_reply only prepended a technical warning, so the API
  response kept the model's misleading "销量价格刷新也已启动" text and did not
  contain the required "商品库刷新成功，销量价格刷新失败" summary.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)


class _FakeOpenAIClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


class _FakeCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            tool_calls = [
                _tool_call("call_products", "run_workflow", {
                    "workflow": "wf2_products_v2",
                    "followup_prompt": "帮我刷新商品库和销量价格",
                }),
                _tool_call("call_sales", "run_workflow", {
                    "workflow": "wf2_sales_v2",
                    "followup_prompt": "帮我刷新商品库和销量价格",
                }),
            ]
            return SimpleNamespace(choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(content=None, tool_calls=tool_calls),
                )
            ])

        # Red-team model text: it falsely says the failed item also started.
        return SimpleNamespace(choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content="ERP 商品库刷新已启动（任务 abc12345），销量价格刷新也已启动。",
                    tool_calls=None,
                ),
            )
        ])


def _tool_call(call_id: str, name: str, args: dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
    )


def _fake_exec_tool(tool_name, tool_args, user=None):
    assert tool_name == "run_workflow", f"unexpected tool: {tool_name}"
    workflow = tool_args.get("workflow")
    if workflow == "wf2_products_v2":
        return {
            "ok": True,
            "task_id": "abc12345",
            "workflow": workflow,
            "label": "ERP 商品库",
            "total_steps": 1,
            "affected_modules": ["sales"],
            "followup_prompt": tool_args.get("followup_prompt"),
        }
    if workflow == "wf2_sales_v2":
        return {
            "ok": False,
            "workflow": workflow,
            "label": "销量价格刷新",
            "error": "permission_denied",
        }
    return {"ok": False, "workflow": workflow, "error": f"unexpected workflow: {workflow}"}


def test_api_chat_rewrites_mixed_products_success_sales_failure():
    from fastapi.testclient import TestClient

    saved_env = {k: os.environ.get(k) for k in ("AUTH_LOCKDOWN", "DB_URL", "LLM_PROVIDER")}
    os.environ["AUTH_LOCKDOWN"] = "0"
    os.environ.pop("DB_URL", None)
    os.environ["LLM_PROVIDER"] = "deepseek"

    from hipop.server.main import app  # noqa: F401 - loads server.* namespace
    import server.agent as _sagent
    import server.data as _sdata
    import server._provider_openai as _openai

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    original_db = _sdata.DB_PATH
    original_exec_tool = _sagent._exec_tool
    original_get_client = _openai._get_client_and_model
    _sdata.DB_PATH = tmp.name
    if hasattr(_sdata, "_feedback_ready"):
        _sdata._feedback_ready = False
    if hasattr(_sdata, "_task_tables_checked"):
        _sdata._task_tables_checked = False
    _sagent._exec_tool = _fake_exec_tool
    _openai._get_client_and_model = lambda _provider: (_FakeOpenAIClient(), "fake-model")

    try:
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "帮我刷新商品库和销量价格"}],
            "scope": {"store": "KSA"},
        })
        assert resp.status_code == 200, f"/api/chat returned {resp.status_code}: {resp.text[:300]}"
        payload = resp.json()

        reply = payload.get("reply") or ""
        assert "商品库刷新成功，销量价格刷新失败" in reply, \
            f"API reply should contain mixed human summary: {reply}"
        assert "销量价格刷新也已启动" not in reply and "也已启动" not in reply, \
            f"API reply must not preserve failed-item success claim: {reply}"
        assert "abc12345" in reply, f"success task_id should remain visible: {reply}"
        assert "permission_denied" in reply, f"failure reason should remain visible: {reply}"

        tasks = payload.get("workflow_tasks") or []
        assert len(tasks) == 2, f"expected two workflow task cards: {tasks}"
        products = next((t for t in tasks if t.get("workflow") == "wf2_products_v2"), None)
        sales = next((t for t in tasks if t.get("workflow") == "wf2_sales_v2"), None)
        assert products and products.get("ok") is True and products.get("task_id") == "abc12345", \
            f"products refresh should be success with real task id in response: {products}"
        assert sales and sales.get("ok") is False and sales.get("error") == "permission_denied", \
            f"sales refresh should be explicit failed item in response: {sales}"
    finally:
        _openai._get_client_and_model = original_get_client
        _sagent._exec_tool = original_exec_tool
        _sdata.DB_PATH = original_db
        if hasattr(_sdata, "_feedback_ready"):
            _sdata._feedback_ready = False
        if hasattr(_sdata, "_task_tables_checked"):
            _sdata._task_tables_checked = False
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


if __name__ == "__main__":
    import traceback

    tests = [test_api_chat_rewrites_mixed_products_success_sales_failure]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✓ {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)
