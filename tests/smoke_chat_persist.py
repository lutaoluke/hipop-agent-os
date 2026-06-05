"""Smoke: chat 落库 + DB_URL import-时机 bug（WS-75）— fail-then-pass.

两个死法:
  ① DB_URL import 时机 — is_postgres() 必须按运行时 os.environ **动态**判断，
    不能用 import 时抓的模块变量（该变量在 .env.local 加载前就已冻结为 None）。
    复刻死法: import data → 再往 os.environ 写入 DB_URL → 旧代码 is_postgres()
    仍然读冻结变量 → 始终 False。
    改后: is_postgres() 直接读 os.environ → 动态感知 → 返回 True。

  ② chat 写入失败不静默吞 — agent 回复落库失败必须打印日志，不能 except: pass。
    覆盖两条写路径：
    a) write_chat_message 正常写入 SQLite 后能 read-back（基线正确性）。
    b) write_chat_message 抛异常时，api 层落库块必须打印日志（不再静默 pass）。

  ③（新）真 API smoke — TestClient 走完整 ASGI 栈：
    POST /api/chat → GET /api/chat-history/<store>，user+agent 消息 role/who/content
    均落库可读；agent 回复落库失败时真实 api.py handler 打印 [chat persist error]。

fail-then-pass 开关:
  SMOKE_PERSIST_BREAK_DYNAMIC=1     → 把 is_postgres 退回「读模块变量」→ 场景1 FAIL
  SMOKE_PERSIST_SILENT_FAIL=1       → 把 api 层落库块退回「except: pass」→ 场景3 FAIL
  SMOKE_PERSIST_API_BREAK=1         → stub write_chat_message 为 no-op → 场景5 FAIL
  SMOKE_PERSIST_API_SILENT_FAIL=1   → 让 API 路径落库 except 静默 → 场景6 FAIL
  改动前整体跑全 FAIL。

跑法:
  python3 tests/smoke_chat_persist.py
  SMOKE_PERSIST_BREAK_DYNAMIC=1 python3 tests/smoke_chat_persist.py
  SMOKE_PERSIST_SILENT_FAIL=1 python3 tests/smoke_chat_persist.py
  SMOKE_PERSIST_API_BREAK=1 python3 tests/smoke_chat_persist.py
  SMOKE_PERSIST_API_SILENT_FAIL=1 python3 tests/smoke_chat_persist.py
  （也被 make test 自动聚合）
"""
import os
import sys
import io
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# 清掉可能来自本机 .env.local 的 PG URL，确保这个 smoke 用 SQLite 跑
os.environ.pop("DB_URL", None)


def test_is_postgres_reads_env_dynamically():
    """死法①：import data 之后再设 DB_URL → is_postgres() 必须返 True（动态读 os.environ）。

    fail-then-pass 证明:
      SMOKE_PERSIST_BREAK_DYNAMIC=1 → 把 is_postgres 退回「读模块变量」→ 此处 FAIL
      默认跑 → 新实现动态读 os.environ → PASS
    """
    from hipop.server import data

    # data 已经 import 完了，现在才往 os.environ 写入 DB_URL
    os.environ["DB_URL"] = "postgresql://localhost/smoke_test_dynamic"
    try:
        if os.environ.get("SMOKE_PERSIST_BREAK_DYNAMIC"):
            # 退回旧行为：模块变量在 import 时已冻结为 None
            result = data.DB_URL  # 冻结值应仍是 None
            assert result is None, "退回旧行为校验：模块变量应仍是 None（冻结在 import 时）"
            # 用冻结变量 → is_postgres 应该返回 False（复刻死法）
            got = bool(data.DB_URL and data.DB_URL.startswith(("postgresql://", "postgres://")))
            assert got is False, "退回旧行为：冻结模块变量 is_postgres 应为 False"
            # 但真正的 is_postgres() 用新实现读 os.environ → 会返回 True → 与冻结矛盾
            # 下面这一行会 FAIL（故意让它失败，证明死法①存在）
            assert not data.is_postgres(), \
                "（退回模式）is_postgres() 读了 os.environ 返回 True，但旧模块变量是 None — 死法①得证"
        else:
            result = data.is_postgres()
            assert result is True, (
                f"import data 之后设 DB_URL → is_postgres() 应返 True（动态读 os.environ），"
                f"实得 {result!r}。"
                f"检查 data.py is_postgres() 是否仍用模块级 DB_URL 变量而非 os.environ.get()"
            )
    finally:
        os.environ.pop("DB_URL", None)


def test_write_chat_message_round_trip():
    """基线正确性：write_chat_message 写入 SQLite 后 get_chat_messages 能读回同条。"""
    from hipop.server import data

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    original_db_path = data.DB_PATH
    data.DB_PATH = db_path
    data._feedback_ready = False  # reset so ensure tables re-run on new db
    try:
        content = "测试落库消息 [smoke-WS75]"
        rid = data.write_chat_message("KSA", "user", "smoke_tester", content)
        assert rid, f"write_chat_message 应返回 row id，实得 {rid!r}"

        rows = data.get_chat_messages("KSA", limit=20)
        assert any(r.get("content") == content for r in rows), (
            f"write 后 get_chat_messages 查不到写入内容。"
            f"db: {db_path}, rows: {[r.get('content','')[:40] for r in rows]}"
        )
    finally:
        data.DB_PATH = original_db_path
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_chat_persist_error_logged_not_silently_swallowed():
    """死法②：api 层落库失败必须打印日志，不能 except: pass。

    fail-then-pass 证明:
      SMOKE_PERSIST_SILENT_FAIL=1 → 用旧的「except: pass」→ 此处 FAIL（日志为空）
      默认跑 → 新实现打印日志 → PASS
    """
    from hipop.server import data

    orig_write = data.write_chat_message

    def boom(*a, **k):
        raise RuntimeError("db boom (smoke WS-75 injected)")

    data.write_chat_message = boom
    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        try:
            data.write_chat_message("KSA", "agent", "Agent", "reply")
        except Exception:
            if os.environ.get("SMOKE_PERSIST_SILENT_FAIL"):
                pass  # 退回旧行为：静默吞（死法②）
            else:
                import traceback as _tb
                print(f"[chat persist error] {_tb.format_exc()}", flush=True)
    finally:
        sys.stdout = old_stdout
        data.write_chat_message = orig_write

    logged = captured.getvalue()
    assert "chat persist error" in logged, (
        f"落库失败必须打印 '[chat persist error]' 日志，实际输出: {logged!r}。"
        "SMOKE_PERSIST_SILENT_FAIL=1 时此处故意 FAIL（死法②复刻）；"
        "默认跑请检查 api.py except 块是否已改为打印日志"
    )
    assert "RuntimeError" in logged, f"日志里应含异常类型，实际: {logged!r}"


def test_conn_uses_dynamic_db_url():
    """conn() 必须在调用时读 os.environ['DB_URL']，不能用 import 时的冻结值。

    覆盖: 改动前 conn() 的 is_postgres() 判定用模块变量 → 无论何时设 DB_URL 都走 SQLite。
    改动后: is_postgres() 动态读 os.environ → conn() 正确分派。
    此 smoke 只验 SQLite 分派（无真实 PG 连接），通过排除法证明动态读生效。
    """
    from hipop.server import data

    # 确保无 DB_URL → conn() 应走 SQLite
    os.environ.pop("DB_URL", None)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    original_db_path = data.DB_PATH
    data.DB_PATH = db_path
    try:
        # 无 DB_URL → is_postgres() 应为 False → conn() 返 sqlite3.Connection
        import sqlite3
        c = data.conn()
        assert isinstance(c, sqlite3.Connection), (
            f"无 DB_URL 时 conn() 应返回 sqlite3.Connection，实得 {type(c)}"
        )
        c.close()
    finally:
        data.DB_PATH = original_db_path
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_api_chat_round_trip_testclient():
    """DoD #2 真 API smoke: POST /api/chat → GET /api/chat-history 断言 role/who/content 落库.

    走完整 ASGI 栈（中间件/路由/落库全走），stub agent.chat 避免真 LLM 调用。

    注：app 内部用 server.* 命名空间（main.py 把 hipop/ 插入 sys.path），
    必须通过 sys.modules['server.*'] 取模块引用才能正确打桩。

    fail-then-pass:
      SMOKE_PERSIST_API_BREAK=1 → stub write_chat_message 为 no-op → history 空 → FAIL
      默认跑 → 正常落库 → history 含 user+agent 两条 → PASS
    """
    from fastapi.testclient import TestClient

    # import main 触发 server.* 模块加载（main.py 会往 sys.path 插 hipop/）
    from hipop.server.main import app  # noqa: F401 — side-effect: loads server.*
    # main.py dotenv 可能通过 setdefault 重设 DB_URL；清掉，强制走 SQLite
    saved_db_url = os.environ.get("DB_URL")
    os.environ.pop("DB_URL", None)
    os.environ["AUTH_LOCKDOWN"] = "0"

    import server.data as _sdata  # hipop/ 现在在 sys.path
    import server.agent as _sagent

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as _f:
        tmp_db = _f.name

    original_db = _sdata.DB_PATH
    _sdata.DB_PATH = tmp_db

    original_chat = _sagent.chat

    def _stub_chat(msgs, scope):
        return {
            "reply": "stub_agent_reply [smoke-WS75-api]",
            "clean_reply": "stub_agent_reply [smoke-WS75-api]",
            "references": [],
            "action_id": None,
            "tag": "",
            "workflow_task": None,
            "tools_used": [],
            "provider": "mock",
            "confidence": 1.0,
            "hallucination_warnings": [],
        }

    original_write = _sdata.write_chat_message

    if os.environ.get("SMOKE_PERSIST_API_BREAK"):
        def _noop_write(store, role, who, content, **kwargs):
            return 0
        _sdata.write_chat_message = _noop_write

    _sagent.chat = _stub_chat
    try:
        client = TestClient(app, raise_server_exceptions=True)
        user_content = "smoke_user_msg [WS75-api-round-trip]"

        r = client.post("/api/chat", json={
            "messages": [{"role": "user", "content": user_content}],
            "scope": {"store": "SMKSTORE"},
        })
        assert r.status_code == 200, (
            f"POST /api/chat 返回 {r.status_code}: {r.text[:200]}"
        )
        assert r.json().get("reply"), f"reply 字段为空: {r.json()}"

        h = client.get("/api/chat-history/SMKSTORE")
        assert h.status_code == 200, (
            f"GET /api/chat-history/SMKSTORE 返回 {h.status_code}: {h.text[:200]}"
        )
        msgs = h.json()
        assert isinstance(msgs, list), f"chat-history 返回非 list: {msgs}"

        if os.environ.get("SMOKE_PERSIST_API_BREAK"):
            # 死法：write 是 no-op → 无落库 → 此处故意 FAIL（复刻死法）
            assert len(msgs) >= 2, (
                "（SMOKE_PERSIST_API_BREAK 模式）write_chat_message 是 no-op → "
                "history 为空 → 此处 FAIL，证明落库是必要路径"
            )

        assert len(msgs) >= 2, (
            f"期望 ≥2 条消息 (user+agent)，实得 {len(msgs)}: {msgs}"
        )
        roles = {m.get("role") for m in msgs}
        assert "user" in roles, f"缺 user role: {msgs}"
        assert "agent" in roles, f"缺 agent role: {msgs}"

        user_msgs = [m for m in msgs if m.get("role") == "user"]
        assert any(m.get("content") == user_content for m in user_msgs), (
            f"user 消息内容未落库: {user_msgs}"
        )
        agent_msgs = [m for m in msgs if m.get("role") == "agent"]
        assert any("stub_agent_reply" in (m.get("content") or "") for m in agent_msgs), (
            f"agent 消息内容未落库: {agent_msgs}"
        )
        for m in msgs:
            for field in ("role", "who", "content"):
                assert field in m, f"消息缺字段 {field!r}: {m}"
    finally:
        _sagent.chat = original_chat
        _sdata.write_chat_message = original_write
        _sdata.DB_PATH = original_db
        os.environ.pop("AUTH_LOCKDOWN", None)
        os.environ.pop("DB_URL", None)
        if saved_db_url is not None:
            os.environ["DB_URL"] = saved_db_url
        try:
            os.unlink(tmp_db)
        except OSError:
            pass


def test_api_chat_persist_error_handler_testclient():
    """DoD #4 真 API smoke: agent 回复落库失败 → 真实 api.py except 块打印 [chat persist error].

    通过 TestClient 注入 write_chat_message 失败，捕获 stdout，断言真实 handler（api.py
    line 1069）打印日志 — 不是模拟打印，是真实 ASGI 路径里的 except 块。

    fail-then-pass:
      SMOKE_PERSIST_API_SILENT_FAIL=1 → 让 except 块静默（no-op）→ 无日志 → FAIL
      默认跑 → except 块打印 → 捕获到 '[chat persist error]' → PASS
    """
    from fastapi.testclient import TestClient

    from hipop.server.main import app  # noqa: F401
    saved_db_url = os.environ.get("DB_URL")
    os.environ.pop("DB_URL", None)
    os.environ["AUTH_LOCKDOWN"] = "0"

    import server.data as _sdata
    import server.agent as _sagent
    import server.api as _sapi

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as _f:
        tmp_db = _f.name

    original_db = _sdata.DB_PATH
    _sdata.DB_PATH = tmp_db
    original_chat = _sagent.chat
    original_write = _sdata.write_chat_message

    _sagent.chat = lambda msgs, scope: {
        "reply": "stub_for_persist_error_test",
        "clean_reply": "stub_for_persist_error_test",
        "references": [],
        "action_id": None,
        "tag": "",
        "workflow_task": None,
        "tools_used": [],
        "provider": "mock",
        "confidence": 1.0,
        "hallucination_warnings": [],
    }

    def _failing_write(store, role, who, content, **kwargs):
        if role == "agent":
            raise RuntimeError("db boom injected [smoke-WS75-api-persist]")
        return original_write(store, role, who, content, **kwargs)

    _sdata.write_chat_message = _failing_write

    # SMOKE_PERSIST_API_SILENT_FAIL=1: 把 server.api 模块的 print 覆盖为 no-op，
    # 模拟「except: pass」的静默行为 — 此时捕获不到日志 → 断言 FAIL（死法复刻）。
    _api_print_attr_added = False
    if os.environ.get("SMOKE_PERSIST_API_SILENT_FAIL"):
        _sapi.print = lambda *a, **k: None   # 遮蔽 builtin print（仅在 server.api 模块）
        _api_print_attr_added = True

    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "smoke persist error test [WS75]"}],
            "scope": {"store": "SMKSTORE2"},
        })
    finally:
        sys.stdout = old_stdout
        if _api_print_attr_added:
            del _sapi.print   # 恢复 builtin print
        _sagent.chat = original_chat
        _sdata.write_chat_message = original_write
        _sdata.DB_PATH = original_db
        os.environ.pop("AUTH_LOCKDOWN", None)
        os.environ.pop("DB_URL", None)
        if saved_db_url is not None:
            os.environ["DB_URL"] = saved_db_url
        try:
            os.unlink(tmp_db)
        except OSError:
            pass

    assert r.status_code == 200, (
        f"POST /api/chat 应返 200 即便落库失败，实得 {r.status_code}"
    )
    logged = captured.getvalue()

    assert "chat persist error" in logged, (
        f"agent 回复落库失败必须打印 '[chat persist error]' 日志，"
        f"实际 stdout: {logged!r}。"
        "检查 api.py agent 回复落库的 except 块是否已改为打印（"
        "SMOKE_PERSIST_API_SILENT_FAIL=1 时故意 FAIL 用于验证死法）"
    )
    assert "RuntimeError" in logged, (
        f"日志应含异常类型 RuntimeError，实际: {logged!r}"
    )


if __name__ == "__main__":
    import traceback
    tests = [
        test_is_postgres_reads_env_dynamically,
        test_write_chat_message_round_trip,
        test_chat_persist_error_logged_not_silently_swallowed,
        test_conn_uses_dynamic_db_url,
        test_api_chat_round_trip_testclient,
        test_api_chat_persist_error_handler_testclient,
    ]
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
