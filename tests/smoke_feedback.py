"""Smoke test: chat agent 常驻「反馈/需求捕获」必须真生效（WS-26）。

钉死两条死法（issue 风险栏点名的）：
  1) 占位假数据 —— capture_feedback 必须**真写 feedback 表**且能回读到那一条；
     写失败时必须返 ok=False+error，**绝不假装记了**（报告即事实）。
  2) 接线缺失 —— capture_feedback 必须真接进 TOOL_FUNCS + TOOLS schema；
     撞限（做不到/超范围）的回复必须**确定性**补一句 offer（不靠 LLM 自觉），
     正常回答路径绝不被污染。

外加红队：feedback 必须按 tenant 隔离（越权串租户查不到别家需求）。

跑法（与 smoke_governance 同套路，PG）：
  python3 tests/smoke_feedback.py
  或 make test（自动聚合）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

os.environ.setdefault("DB_URL", "postgresql://hipop:hipop_dev_password@localhost:5432/hipop")
os.environ.setdefault("JWT_SECRET", "hipop_alpha_stable_secret_keep_this")


def _set_chat_ctx(tid=1, store="KSA", user="smoke_tester", role="运营"):
    from hipop.server import agent, data
    agent._chat_tenant.set(tid)
    agent._chat_scope.set({"store": store, "current_user": user, "current_role": role,
                           "tenant_id": tid})
    data.set_current_tenant(tid)


def test_capture_feedback_really_writes_and_reads_back():
    """死法·占位假数据：capture_feedback 必须真落库，且回读到该条原话。"""
    from hipop.server import agent, data
    _set_chat_ctx(tid=1)
    content = "希望能把销量导出成 PDF 月报自动发邮件 [smoke-marker-WS26]"
    res = agent.tool_capture_feedback(content=content, scene="问能不能导 PDF 月报",
                                      category="需求")
    assert res.get("ok") is True, f"capture_feedback 应成功落库，实得: {res}"
    fid = res.get("feedback_id")
    assert fid, f"返回里应带 feedback_id: {res}"
    # 直查库证明真写了这一条（不信 tool 自报）
    rows = data._fetch(
        "SELECT id, content, category FROM feedback WHERE tenant_id=? AND id=?",
        (1, fid),
    )
    assert len(rows) == 1, f"feedback 表里查不到刚写的 #{fid}（占位假数据？）"
    assert rows[0]["content"] == content, "落库内容与用户原话不一致"


def test_capture_feedback_reports_error_on_write_failure():
    """报告即事实：写不进库时必须返 ok=False+error，绝不假装记了。"""
    from hipop.server import agent, data
    _set_chat_ctx(tid=1)
    orig = data.write_feedback

    def _boom(*a, **k):
        raise RuntimeError("db down (smoke injected)")

    data.write_feedback = _boom
    try:
        res = agent.tool_capture_feedback(content="写库会炸的需求", category="需求")
    finally:
        data.write_feedback = orig
    assert res.get("ok") is False, f"写失败必须 ok=False，绝不能假装成功: {res}"
    assert res.get("error"), f"写失败必须带 error 字段: {res}"
    assert "feedback_id" not in res or not res.get("feedback_id"), \
        "写失败不许返一个假的 feedback_id"


def test_capture_feedback_wired_into_tooling():
    """死法·接线缺失：capture_feedback 必须既在 TOOL_FUNCS 又在 TOOLS schema。"""
    from hipop.server import agent
    assert "capture_feedback" in agent.TOOL_FUNCS, "capture_feedback 没接进 TOOL_FUNCS"
    schema_names = {t["name"] for t in agent.TOOLS}
    assert "capture_feedback" in schema_names, "capture_feedback 没进 TOOLS schema（LLM 看不到）"
    # 必须能被 _exec_tool 真派发到（不是裸 key）
    spec = next(t for t in agent.TOOLS if t["name"] == "capture_feedback")
    assert "content" in spec["input_schema"]["properties"], "capture_feedback schema 缺 content 参数"


def test_deadend_reply_gets_feedback_offer():
    """验收①：回复『做不到/超范围』时确定性补一句 offer（不靠 LLM 自觉）。"""
    from hipop.server import agent
    deadends = [
        "抱歉，我暂时做不了把数据导成 PPT 这件事。",
        "这个功能系统目前不支持。",
        "改商品价格超出我的能力范围。",
    ]
    for r in deadends:
        out = agent._maybe_append_feedback_offer(r, [])
        assert agent._OFFER_MARK in out, f"撞限回复没补 offer: {r!r} → {out!r}"
        assert out.startswith(r.rstrip()[:6]), "offer 应追加在原文之后，不该替换原文"


def test_normal_reply_not_polluted_by_offer():
    """验收④：正常回答路径绝不被 offer 污染。"""
    from hipop.server import agent
    normals = [
        "TBA0210A 趋势下滑，建议本周补 200 件。",
        "KSA 店铺今天 3 个红色告警，PDZ0027158 卡仓 5 天，建议约仓。",
        "这个 SKU 库存还能撑 12 天，暂时不用补。",   # 含『不』但不是撞限
    ]
    for r in normals:
        out = agent._maybe_append_feedback_offer(r, ["query_sku"])
        assert out == r, f"正常回复被误注入 offer: {r!r} → {out!r}"


def test_offer_not_repeated_after_capture():
    """已调 capture_feedback（已记下）后，不再重复 offer。"""
    from hipop.server import agent
    r = "我做不了这个，不过我可以帮你记下来。"
    out = agent._maybe_append_feedback_offer(r, ["capture_feedback"])
    assert out == r, "已经记过需求了不该再 offer"


def test_feedback_tenant_isolation():
    """红队·越权串租户：tenant=2 写的需求，tenant=1 查不到。"""
    from hipop.server import agent, data
    # tenant 2 写一条
    _set_chat_ctx(tid=2)
    marker = "ONLY-TENANT-2-NEEDS [smoke-marker-WS26]"
    res2 = agent.tool_capture_feedback(content=marker, category="需求")
    assert res2.get("ok") is True, f"tenant=2 应能写自己的 feedback: {res2}"
    # tenant 1 视角查不到 tenant 2 的需求
    _set_chat_ctx(tid=1)
    leaked = data._fetch(
        "SELECT id FROM feedback WHERE content=?", (marker,)
    )
    assert leaked == [], f"tenant=1 串到了 tenant=2 的需求（RLS 失效）: {leaked}"


def _cleanup_markers():
    """删掉本 smoke 自己写的标记行，别污染 dev 库（按租户分别删，RLS 才放行）。"""
    from hipop.server import data
    for tid in (1, 2):
        data.set_current_tenant(tid)
        try:
            with data.conn() as c:
                c.execute("DELETE FROM feedback WHERE tenant_id=? AND content LIKE ?",
                          (tid, "%smoke-marker-WS26%"))
                c.commit()
        except Exception:
            pass


if __name__ == "__main__":
    import traceback
    tests = [
        test_capture_feedback_really_writes_and_reads_back,
        test_capture_feedback_reports_error_on_write_failure,
        test_capture_feedback_wired_into_tooling,
        test_deadend_reply_gets_feedback_offer,
        test_normal_reply_not_polluted_by_offer,
        test_offer_not_repeated_after_capture,
        test_feedback_tenant_isolation,
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
    _cleanup_markers()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)
