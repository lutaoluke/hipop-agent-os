"""Smoke: chat dynamic expectation prep uses the authenticated session.

FAIL before WS-143:
  `smoke_chat.py` logged in, but `_prepare_dynamic_expectations()` fetched
  `/api/sku-metrics/...` through a fresh opener with no auth cookies. On a cold
  run without `HIPOP_AUTH_TOKEN`, the prep step 401'd before STALE_TST001 ran.

PASS after WS-143:
  Dynamic prep reuses the login opener and separately classifies STALE_TST001 as
  missing vs found-but-fail-closed. Missing still expects a not-found reply;
  stale/source-failed still expects a fail-closed freshness reply.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import urlparse


REPO = Path(__file__).resolve().parents[1]
SMOKE_CHAT = REPO / "tests" / "smoke_chat.py"


class _JsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class _AuthenticatedOpener:
    def __init__(self, stale_item):
        self.stale_item = stale_item
        self.calls: list[str] = []

    def open(self, req, timeout=0):
        parsed = urlparse(req.full_url)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        self.calls.append(path)
        if path == "/api/sku-health/KSA?listing=all&limit=10000":
            return _JsonResponse([
                {"partner_sku": "A", "product_id": "P1", "is_listed": True},
                {"partner_sku": "B", "product_id": "P2", "is_listed": False},
            ])
        if path == "/api/today/KSA":
            return _JsonResponse({"sku_count": 1})
        if path == "/api/sku-metrics/KSA/TBB0116A":
            return _JsonResponse({"items": []})
        if path == "/api/sku-metrics/KSA/STALE_TST001":
            return _JsonResponse({"items": [self.stale_item]})
        raise AssertionError(f"unexpected request path: {path}")


def _load_smoke_chat():
    spec = importlib.util.spec_from_file_location("smoke_chat_under_test", SMOKE_CHAT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _prepare_with_item(stale_item):
    mod = _load_smoke_chat()
    case = mod._find_case("T04 快照过期")
    assert case is not None
    case.name = "T04 快照过期边界（seeded as missing before dynamic prep）"
    case.must_contain = [mod._STALE_TST001_MISSING_RE]

    opener = _AuthenticatedOpener(stale_item)
    mod._prepare_dynamic_expectations("http://hipop.local", opener)
    case = mod._find_case("T04 快照过期")
    return mod, case, opener.calls


def test_dynamic_prep_uses_authenticated_opener():
    mod, _case, calls = _prepare_with_item({
        "sku": "STALE_TST001",
        "found": True,
        "data_stale": True,
    })
    assert calls == [
        "/api/sku-health/KSA?listing=all&limit=10000",
        "/api/today/KSA",
        "/api/sku-metrics/KSA/TBB0116A",
        "/api/sku-metrics/KSA/STALE_TST001",
    ], f"dynamic prep did not use the supplied opener for every endpoint: {calls}"
    assert mod._STALE_TST001_STALE_RE


def test_stale_tst001_found_fail_closed_is_not_classified_as_missing():
    for stale_item in (
        {"sku": "STALE_TST001", "found": True, "data_stale": True},
        {"sku": "STALE_TST001", "found": True, "data_stale": False, "live_sales_failed": True},
    ):
        mod, case, _calls = _prepare_with_item(stale_item)
        assert case.must_contain == [mod._STALE_TST001_STALE_RE], (
            f"found fail-closed STALE_TST001 should expect stale/fail-closed wording, got {case.must_contain}"
        )
        assert "fail-closed" in case.name


def test_stale_tst001_missing_still_requires_not_found_reply():
    mod, case, _calls = _prepare_with_item({
        "sku": "STALE_TST001",
        "found": False,
    })
    assert case.must_contain == [mod._STALE_TST001_MISSING_RE], (
        f"missing STALE_TST001 must not be allowed by stale wording: {case.must_contain}"
    )
    assert "不存在" in case.name


def run():
    failures = []
    for fn in [
        test_dynamic_prep_uses_authenticated_opener,
        test_stale_tst001_found_fail_closed_is_not_classified_as_missing,
        test_stale_tst001_missing_still_requires_not_found_reply,
    ]:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"  [FAIL] {fn.__name__}\n         ↳ {type(e).__name__}: {e}")
    if failures:
        print(f"✗ {len(failures)} failures")
        return 1
    print("✓ chat dynamic expectation auth/freshness boundary smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
