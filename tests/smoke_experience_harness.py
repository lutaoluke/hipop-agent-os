"""Smoke：WS-156 体验官 harness 契约 —— fail-then-pass 承重墙。

钉死 `hipop/runtime/experience_harness.py` 这条链:**真实浏览器回放 → 第二通道对账 →
证据 bundle → 收敛结论 → 自动落体验问题 issue**。核心断言在 `tests/test_phase1.py`,本文件
是 `make test` 自动发现入口 + fail-then-pass 演示。

承重门(`conclude()`)只有一条 PASS 路径:双通道证据齐全且对账一致。任何"缺浏览器证据"
判 INVALID、"缺对账"判 INCONCLUSIVE、"对不上"判 FAIL —— prompt 说"我体验过了"不算数。

fail-then-pass（两个 env 开关复刻两种死法，证明门是承重的、不是装饰）：
  - 默认跑 → 全过（门正确收敛四种结论 + 落 issue/去重）。
  - SMOKE_WS156_BYPASS_GATE=1 → 把 conclude 退回"prompt 驱动体验官"的死代码短路版
    (只要回放产出过任何文本就 PASS,不看截图/trace/对账)→「缺浏览器证据必 INVALID」
    「缺对账必 INCONCLUSIVE」断言 FAIL。复刻死法①接线缺失 + ②死代码短路:harness 写了但
    判定仍靠"跑过一下就算"。
  - SMOKE_WS156_FAKE_EVIDENCE=1 → 让"present()"无视磁盘文件是否存在(冒充截图/trace)
    → 「截图/trace 文件不存在必 INVALID」断言 FAIL。复刻死法③占位假数据:证据 bundle 里
    填了路径但磁盘上根本没有截图。
  改动前(experience_harness.py 还不存在)整个文件 ImportError 即全 fail。

跑法：
  python3 tests/smoke_experience_harness.py
  SMOKE_WS156_BYPASS_GATE=1   python3 tests/smoke_experience_harness.py   # 看回归 fail
  SMOKE_WS156_FAKE_EVIDENCE=1 python3 tests/smoke_experience_harness.py   # 看回归 fail
  （也被 make test 自动聚合）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import test_phase1
from hipop.runtime import experience_harness as eh


def _apply_regressions():
    """按 env 开关把 harness 退回死法版本，演示门若被绕过/证据若可伪造，断言会变红。"""
    if os.environ.get("SMOKE_WS156_BYPASS_GATE") == "1":
        # 死法①②：prompt 驱动体验官 —— 只要回放产出过任何文本就 PASS，不看证据/对账。
        def _legacy_conclude(case, browser, channel):
            bundle = eh.EvidenceBundle(
                case=case, browser=browser, channel=channel,
                reconciliation=eh.Reconciliation(),
            )
            bundle.conclusion = eh.PASS if (browser.rendered_text or "").strip() else eh.INVALID
            return bundle
        eh.conclude = _legacy_conclude

    if os.environ.get("SMOKE_WS156_FAKE_EVIDENCE") == "1":
        # 死法③：占位假数据 —— present() 不再校验磁盘文件，路径填了就当证据齐全。
        def _fake_present(self):
            return not self.error and bool((self.rendered_text or "").strip())
        eh.BrowserEvidence.present = _fake_present


def run():
    _apply_regressions()
    tests = [
        test_phase1.test_ws156_conclude_pass_requires_both_channels,
        test_phase1.test_ws156_missing_browser_evidence_is_invalid_never_pass,
        test_phase1.test_ws156_missing_second_channel_is_inconclusive,
        test_phase1.test_ws156_mismatch_is_fail_with_evidence_summary,
        test_phase1.test_ws156_hallucination_in_browser_render_is_fail,
        test_phase1.test_ws156_sync_experience_issue_creates_then_dedupes,
        test_phase1.test_ws156_run_case_wires_drivers_into_gate,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__} 异常: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n✗ WS-156 体验官 harness smoke：{failed}/{len(tests)} 失败")
        return 1
    print("\n✓ WS-156 体验官 harness smoke 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(run())
