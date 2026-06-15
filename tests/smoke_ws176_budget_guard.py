"""Smoke: WS-176 budget guard deterministic routing contract.

This file exists so `make test` picks up the WS-176 phase1 tests without
touching the Makefile hotspot. The detailed assertions live in test_phase1.py
per the assignment.
"""
from __future__ import annotations

import importlib.util
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)


def _load_phase1():
    path = os.path.join(HERE, "test_phase1.py")
    spec = importlib.util.spec_from_file_location("test_phase1", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    phase1 = _load_phase1()
    phase1.test_ws176_budget_guard_r1_r2_agent_tier_rollup()
    print("✓ R1/R2 daily guard and agent/tier rollup")
    phase1.test_ws176_budget_guard_monthly_session_concentration_rules()
    print("✓ monthly cap, session probe, and concentration guard")
    phase1.test_ws176_budget_guard_dry_run_smoke_and_workflow_wiring()
    print("✓ dry-run smoke and workflow/verifier wiring")
    print("\n3/3 passed")


if __name__ == "__main__":
    main()
