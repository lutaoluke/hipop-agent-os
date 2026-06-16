"""LIVE（手动跑，需本机起 workbench :8765 + 装 playwright）—— WS-156 真浏览器回放。

刻意不叫 `smoke_*.py`：`make test` 只自动发现 `tests/smoke_*.py`，本文件**不**进 CI
全量（CI 无浏览器/无 server 会硬失败）。它是验收 #1 的「真实浏览器回放 + 第二通道对账
产出结论」端到端证明，由体验官在本机活 workbench 下跑、把命令与结论回贴 issue。

它做的事 = 体验官以用户身份在真 Chromium 里发 chat、采集用户**实际看到**的渲染 + 截图 +
trace，独立直连 /api/chat 取后端权威结果做第二通道，对账后由承重门收敛结论；FAIL 时按规则
落体验问题 issue（需 $HIPOP_EXPERIENCE_STATE_ISSUE）。

  · 无 playwright / workbench 没起 → 浏览器证据缺失 → 门判 INVALID（不是 PASS，也不报错退出），
    打印结论后 exit 0 跳过。这正是"缺浏览器证据不得 PASS"的活体现。
  · 真跑通 → 打印 PASS/FAIL + 证据 bundle 路径（截图/trace）。

跑法：
  HIPOP_AUTH_TOKEN=<token> python3 tests/live_experience_replay.py
  HIPOP_EXPERIENCE_STATE_ISSUE=<issue> python3 tests/live_experience_replay.py --sync
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from hipop.runtime import experience_harness as eh


CASES = [
    eh.ExperienceCase(
        case_id="overview_ksa_scope",
        prompt="KSA 一共有几个 SKU？列几个出来",
        store="ksa",
        must_contain=[r"\d+\s*个|SKU"],
    ),
]


def main():
    base = os.environ.get("HIPOP_URL", "http://127.0.0.1:8765")
    token = os.environ.get("HIPOP_AUTH_TOKEN", "")
    do_sync = "--sync" in sys.argv

    driver = eh.playwright_browser_driver(base_url=base, auth_token=token)
    channel = eh.api_second_channel(base_url=base, auth_token=token)

    any_fail = False
    for case in CASES:
        bundle = eh.run_case(case, driver, channel)
        print(f"\n▶ {case.case_id} → {bundle.conclusion}")
        print(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2))
        if bundle.conclusion == eh.FAIL:
            any_fail = True
            if do_sync:
                result = eh.sync_experience_issue(bundle)
                print(f"  落 issue: {result}")

    if any_fail:
        print("\n发现体验问题（FAIL）。")
    else:
        print("\n（无 FAIL；缺浏览器/对账时结论为 INVALID/INCONCLUSIVE，非 PASS。）")
    # 本 live 用于人工观察，不以结论 fail-exit（CI 不跑它）。
    return 0


if __name__ == "__main__":
    sys.exit(main())
