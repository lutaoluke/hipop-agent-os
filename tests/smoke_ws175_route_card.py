"""Smoke：WS-175 route-card CLI contract.

The actual assertions live in tests/test_phase1.py so pytest-style phase tests
and Makefile smoke autodiscovery exercise the same contract.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import test_phase1


def run():
    tests = [
        test_phase1.test_ws175_route_card_show_derives_fields_without_persisting_metadata,
        test_phase1.test_ws175_route_card_bump_dedupe_and_new_events,
        test_phase1.test_ws175_route_card_pool_state_writes_only_persistent_fields,
        test_phase1.test_ws175_route_card_metadata_writes_are_centralized,
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
        print(f"\n✗ WS-175 route-card smoke：{failed}/{len(tests)} 失败")
        return 1
    print("\n✓ WS-175 route-card smoke 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(run())
