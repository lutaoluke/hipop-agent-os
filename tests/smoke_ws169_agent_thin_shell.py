"""Smoke: WS-169 final thin-shell acceptance for hipop/server/agent.py.

WS-166/167/168 already pinned the three major migrations:
tool implementations, deterministic routes/formatters, and prompt text. This
final gate prevents the lock file from quietly becoming a business-logic home
again. `agent.py` may keep the public tool projection, `_exec_tool` governance
funnel, and the `chat()` entrypoint wiring; helper implementations must live in
non-lock modules and be re-exported from `agent`.

Fail-then-pass intent:
  - before WS-169 extraction, this smoke finds many helper `def`s in agent.py
    (`_erp_sku_stats_live`, `_freshness_gate_route`, `_maybe_*`, ...);
  - after extraction, agent.py only defines the shell functions below while
    the old `agent.X` import/patch surface still resolves.

Run:
  python3 tests/smoke_ws169_agent_thin_shell.py
"""
import ast
import inspect
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

AGENT_PY = os.path.join(REPO, "hipop", "server", "agent.py")

ALLOWED_AGENT_DEFS = {"_get_client", "_exec_tool", "chat"}

MOVED_SYMBOLS = {
    "hipop.server._agent_context": {
        "_get_tenant",
        "_resolve_entity_alias",
    },
    "hipop.server._tool_runtime": {
        "_erp_sku_stats_live",
        "_normalize_replenishment_rows",
        "_write_xlsx_and_return",
        "_erp_token_or_error",
        "_patch_wls_token",
        "_fetch_logistics_nodes",
        "_physical_tracking_url",
        "_utc_now_iso",
    },
    "hipop.server._chat_pipeline": {
        "_run_llm_judge",
        "_compute_judge_confidence",
        "_strip_safety_banner",
        "_clean_history",
        "_maybe_append_feedback_offer",
        "_maybe_append_stock_readiness_warning",
        "_ensure_export_download_link",
        "_maybe_inject_missing_rates",
        "_maybe_append_oldest_data_health_date",
        "_maybe_append_order_lookup_negative_hint",
        "_maybe_append_navigation_url",
        "_dedup_refs",
    },
    "hipop.server._chat_workflows": {
        "_current_workflow_task",
        "_existing_workflow_task_id",
        "_workflow_registry_summary",
        "_active_workflow_task",
        "_logistics_task_evidence_check",
        "_extract_freshness_target_date",
        "_detect_operational_domain",
        "_freshness_gate_route",
        "_execute_workflow_route",
        "_msg_text",
        "_inventory_refresh_feasibility",
        "_pending_inventory_refresh_inquiry",
        "_inventory_refresh_no_task_result",
        "_inventory_refresh_confirm_gate",
    },
}


def _top_level_defs(src: str) -> set:
    tree = ast.parse(src)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_agent_py_only_defines_shell_entrypoints():
    src = open(AGENT_PY, encoding="utf-8").read()
    defs = _top_level_defs(src)
    extra = sorted(defs - ALLOWED_AGENT_DEFS)
    assert not extra, (
        "agent.py is no longer a helper implementation file. Move these "
        f"top-level defs to a non-lock module and re-export if needed: {extra}"
    )

    # Detector self-check: adding a fake helper body must trip the same gate.
    fake_defs = _top_level_defs(src + "\n\ndef _freshness_fake_regress():\n    return None\n")
    assert "_freshness_fake_regress" in (fake_defs - ALLOWED_AGENT_DEFS), (
        "thin-shell detector self-check failed; fake helper was not caught"
    )
    print(f"  ✓ agent.py top-level defs are shell-only: {sorted(defs)}")


def test_moved_helpers_keep_agent_reexport_surface():
    from importlib import import_module
    from hipop.server import agent

    checked = 0
    for module_name, names in MOVED_SYMBOLS.items():
        module = import_module(module_name)
        for name in sorted(names):
            a = getattr(agent, name, None)
            m = getattr(module, name, None)
            assert m is not None, f"{module_name}.{name} missing"
            assert a is m, (
                f"agent.{name} no longer re-exports {module_name}.{name}; "
                "existing tests and tool patch points depend on agent.X"
            )
            defining_module = inspect.getmodule(a)
            assert defining_module is not None and defining_module.__name__ == module_name, (
                f"agent.{name} implementation still lives in "
                f"{defining_module.__name__ if defining_module else None}, expected {module_name}"
            )
            checked += 1
    print(f"  ✓ {checked} helper symbols re-export from non-agent modules")


def test_shell_still_contains_required_wiring_points():
    src = open(AGENT_PY, encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_exec_tool" in funcs and "chat" in funcs, "agent shell must expose _exec_tool and chat"

    exec_names = {n.id for n in ast.walk(funcs["_exec_tool"]) if isinstance(n, ast.Name)}
    for name in ("TOOL_FUNCS", "_chat_scope", "_chat_intent", "_get_tenant"):
        assert name in exec_names, f"_exec_tool lost required wiring reference: {name}"

    chat_names = {n.id for n in ast.walk(funcs["chat"]) if isinstance(n, ast.Name)}
    for name in ("SYSTEM_PROMPT", "TOOLS", "TOOL_FUNCS", "_exec_tool", "_freshness_gate_route"):
        assert name in chat_names, f"chat lost required shell wiring reference: {name}"
    print("  ✓ _exec_tool and chat retain governance/provider/runtime wiring")


def run():
    tests = [
        test_agent_py_only_defines_shell_entrypoints,
        test_moved_helpers_keep_agent_reexport_surface,
        test_shell_still_contains_required_wiring_points,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {test.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {test.__name__} exception: {type(e).__name__}: {e}")
    if failed:
        print(f"\n✗ WS-169 thin-shell smoke: {failed}/{len(tests)} failed")
        return 1
    print("\n✓ WS-169 thin-shell smoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
