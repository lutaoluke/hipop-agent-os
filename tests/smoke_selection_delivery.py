"""WS-70 smoke: L4 delivery surfaces and structured selection feedback."""
from __future__ import annotations

from tests.test_phase1 import (
    test_selection_delivery_agent_os_and_report_share_candidate_pool,
    test_selection_delivery_evidence_insufficient_visible_in_agent_os_and_report,
    test_selection_delivery_structured_feedback_writes_preferences_and_changes_offline_state,
    test_selection_feedback_api_requires_login_and_scopes_preferences_by_tenant_store,
)


if __name__ == "__main__":
    test_selection_delivery_agent_os_and_report_share_candidate_pool()
    print("  ✓ test_selection_delivery_agent_os_and_report_share_candidate_pool")
    test_selection_delivery_evidence_insufficient_visible_in_agent_os_and_report()
    print("  ✓ test_selection_delivery_evidence_insufficient_visible_in_agent_os_and_report")
    test_selection_delivery_structured_feedback_writes_preferences_and_changes_offline_state()
    print("  ✓ test_selection_delivery_structured_feedback_writes_preferences_and_changes_offline_state")
    test_selection_feedback_api_requires_login_and_scopes_preferences_by_tenant_store()
    print("  ✓ test_selection_feedback_api_requires_login_and_scopes_preferences_by_tenant_store")
