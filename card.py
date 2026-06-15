#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional


VALID_FAIL_REASONS = {"transient", "quota", "code_error", "review_reject"}
STATE_ISSUE_ENV = "MULTICA_ROUTE_CARD_STATE_ISSUE"
DEDUPE_KEY = "route_card_dedupe_keys"

DERIVED_KEYS = {"author_model", "reviewer_model", "current_tier", "route_pool"}
PERSISTENT_KEYS = {
    "attempt_count",
    "escalation_level",
    "pool_frozen",
    "pool_paused_until",
    "last_fail_reason",
    DEDUPE_KEY,
}


class RouteCardError(RuntimeError):
    pass


class MulticaRunner:
    def json(self, args: List[str]) -> Dict[str, Any]:
        proc = subprocess.run(
            ["multica"] + args + ["--output", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
        text = proc.stdout.strip()
        if not text:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise RouteCardError(f"multica returned non-object JSON for {' '.join(args)}")
        return data

    def run(self, args: List[str]) -> str:
        proc = subprocess.run(
            ["multica"] + args,
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout


def _metadata_object(raw: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(raw.get("metadata"), dict):
        return dict(raw["metadata"])
    return dict(raw)


def _decode_json_container(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return value
    return decoded


def _load_issue(issue: str, runner: Optional[MulticaRunner] = None) -> Dict[str, Any]:
    runner = runner or MulticaRunner()
    return runner.json(["issue", "get", issue])


def _load_metadata(issue: str, runner: Optional[MulticaRunner] = None) -> Dict[str, Any]:
    runner = runner or MulticaRunner()
    return _metadata_object(runner.json(["issue", "metadata", "list", issue]))


def _set_metadata(
    issue: str,
    key: str,
    value: Any,
    runner: Optional[MulticaRunner] = None,
    value_type: Optional[str] = None,
) -> None:
    if key in DERIVED_KEYS:
        raise RouteCardError(f"refusing to persist derived route-card key: {key}")
    if key not in PERSISTENT_KEYS:
        raise RouteCardError(f"refusing to persist unknown route-card key: {key}")
    runner = runner or MulticaRunner()
    if value_type is None and isinstance(value, (dict, list, bool)):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        encoded = str(value)
    args = ["issue", "metadata", "set", issue, "--key", key, "--value", encoded]
    if value_type:
        args += ["--type", value_type]
    runner.run(args)


def _safe_agent_model(agent_id: Optional[str], runner: Optional[MulticaRunner]) -> str:
    if not agent_id:
        return "unknown"
    runner = runner or MulticaRunner()
    try:
        agent = runner.json(["agent", "get", agent_id])
    except Exception:
        return "unknown"
    model = agent.get("model")
    return str(model) if model else "unknown"


def _first_present(data: Dict[str, Any], fields: List[str]) -> Optional[Any]:
    for field in fields:
        value = data.get(field)
        if value:
            return value
    return None


def _reviewer_model(issue: Dict[str, Any], runner: Optional[MulticaRunner]) -> str:
    direct_model = _first_present(issue, ["reviewer_model", "verifier_model", "validator_model"])
    if direct_model:
        return str(direct_model)

    reviewer_id = _first_present(
        issue,
        [
            "reviewer_agent_id",
            "reviewer_id",
            "reviewer_assignee_id",
            "verifier_agent_id",
            "validator_agent_id",
        ],
    )
    if reviewer_id:
        return _safe_agent_model(str(reviewer_id), runner)

    for field in ("reviewer", "verifier", "validator"):
        value = issue.get(field)
        if isinstance(value, dict):
            if value.get("model"):
                return str(value["model"])
            nested_id = _first_present(value, ["agent_id", "id", "assignee_id"])
            if nested_id:
                return _safe_agent_model(str(nested_id), runner)

    reviewers = issue.get("reviewers")
    if isinstance(reviewers, list):
        for value in reviewers:
            if not isinstance(value, dict):
                continue
            if value.get("model"):
                return str(value["model"])
            nested_id = _first_present(value, ["agent_id", "id", "assignee_id"])
            if nested_id:
                return _safe_agent_model(str(nested_id), runner)
    return "unknown"


def derive_current_tier(title: str) -> str:
    if "[难]" in title:
        return "难"
    if "[标准]" in title:
        return "标准"
    return "unknown"


def derive_route_pool(model: str) -> str:
    normalized = (model or "").lower().replace("_", "-").replace(" ", "")
    if "sonnet" in normalized:
        return "Sonnet"
    if "opus" in normalized:
        return "Opus"
    if "gpt-5.5" in normalized or "gpt5.5" in normalized:
        return "GPT5.5"
    if "gpt" in normalized:
        return "GPT"
    return "unknown"


def _persistent_view(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "attempt_count": int(metadata.get("attempt_count") or 0),
        "escalation_level": int(metadata.get("escalation_level") or 0),
        "pool_frozen": _pool_map(metadata.get("pool_frozen")),
        "pool_paused_until": _pool_map(metadata.get("pool_paused_until")),
        "last_fail_reason": metadata.get("last_fail_reason"),
        DEDUPE_KEY: _dedupe_keys(metadata),
    }


def show_card(issue: str, runner: Optional[MulticaRunner] = None) -> Dict[str, Any]:
    runner = runner or MulticaRunner()
    issue_data = _load_issue(issue, runner)
    metadata = _load_metadata(issue, runner)
    author_model = "unknown"
    if issue_data.get("assignee_type") == "agent":
        author_model = _safe_agent_model(issue_data.get("assignee_id"), runner)
    reviewer_model = _reviewer_model(issue_data, runner)
    title = str(issue_data.get("title") or "")
    return {
        "issue": {
            "id": issue_data.get("id"),
            "identifier": issue_data.get("identifier"),
            "title": title,
            "assignee_type": issue_data.get("assignee_type"),
            "assignee_id": issue_data.get("assignee_id"),
        },
        "derived": {
            "author_model": author_model,
            "reviewer_model": reviewer_model,
            "current_tier": derive_current_tier(title),
            "route_pool": derive_route_pool(author_model),
        },
        "persistent": _persistent_view(metadata),
    }


def _dedupe_keys(metadata: Dict[str, Any]) -> List[str]:
    raw = _decode_json_container(metadata.get(DEDUPE_KEY) or [])
    if isinstance(raw, list):
        return [str(v) for v in raw]
    if isinstance(raw, str):
        return [raw]
    return []


def bump_card(
    issue: str,
    reason: str,
    dedupe_key: Optional[str] = None,
    runner: Optional[MulticaRunner] = None,
) -> Dict[str, Any]:
    if reason not in VALID_FAIL_REASONS:
        raise RouteCardError(f"invalid fail reason {reason!r}")
    runner = runner or MulticaRunner()
    metadata = _load_metadata(issue, runner)
    attempt_count = int(metadata.get("attempt_count") or 0)
    keys = _dedupe_keys(metadata)

    if dedupe_key and dedupe_key in keys:
        return {
            "ok": True,
            "deduped": True,
            "issue": issue,
            "attempt_count": attempt_count,
            "last_fail_reason": metadata.get("last_fail_reason"),
            "dedupe_key": dedupe_key,
        }

    attempt_count += 1
    _set_metadata(issue, "attempt_count", attempt_count, runner, value_type="number")
    _set_metadata(issue, "last_fail_reason", reason, runner, value_type="string")
    if dedupe_key:
        keys.append(str(dedupe_key))
        _set_metadata(issue, DEDUPE_KEY, keys, runner)
    return {
        "ok": True,
        "deduped": False,
        "issue": issue,
        "attempt_count": attempt_count,
        "last_fail_reason": reason,
        "dedupe_key": dedupe_key,
    }


def _pool_map(value: Any) -> Dict[str, Any]:
    value = _decode_json_container(value)
    return dict(value) if isinstance(value, dict) else {}


def _state_issue(explicit: Optional[str]) -> str:
    issue = explicit or os.environ.get(STATE_ISSUE_ENV)
    if not issue:
        raise RouteCardError(
            f"pool state needs --state-issue or {STATE_ISSUE_ENV}; "
            "Multica metadata is issue-scoped"
        )
    return issue


def freeze_pool(
    pool: str,
    state_issue: Optional[str] = None,
    runner: Optional[MulticaRunner] = None,
) -> Dict[str, Any]:
    runner = runner or MulticaRunner()
    issue = _state_issue(state_issue)
    metadata = _load_metadata(issue, runner)
    frozen = _pool_map(metadata.get("pool_frozen"))
    frozen[pool] = True
    _set_metadata(issue, "pool_frozen", frozen, runner)
    return {"ok": True, "state_issue": issue, "pool": pool, "pool_frozen": frozen}


def unfreeze_pool(
    pool: str,
    state_issue: Optional[str] = None,
    runner: Optional[MulticaRunner] = None,
) -> Dict[str, Any]:
    runner = runner or MulticaRunner()
    issue = _state_issue(state_issue)
    metadata = _load_metadata(issue, runner)
    frozen = _pool_map(metadata.get("pool_frozen"))
    frozen[pool] = False
    _set_metadata(issue, "pool_frozen", frozen, runner)
    return {"ok": True, "state_issue": issue, "pool": pool, "pool_frozen": frozen}


def _validate_timestamp(ts: str) -> str:
    if not ts or not ts.strip():
        raise RouteCardError("--until must be a non-empty timestamp")
    normalized = ts.strip()
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RouteCardError(f"--until must be ISO-8601/RFC3339-like: {ts!r}") from exc
    return normalized


def pause_pool(
    pool: str,
    until: str,
    state_issue: Optional[str] = None,
    runner: Optional[MulticaRunner] = None,
) -> Dict[str, Any]:
    runner = runner or MulticaRunner()
    issue = _state_issue(state_issue)
    until = _validate_timestamp(until)
    metadata = _load_metadata(issue, runner)
    paused = _pool_map(metadata.get("pool_paused_until"))
    paused[pool] = until
    _set_metadata(issue, "pool_paused_until", paused, runner)
    return {"ok": True, "state_issue": issue, "pool": pool, "pool_paused_until": paused}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="card.py",
        description="Maintain deterministic Multica route-card metadata.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="print a route card with derived and persistent fields")
    show.add_argument("issue")

    bump = sub.add_parser("bump", help="increment attempt_count for a fail/review event")
    bump.add_argument("issue")
    bump.add_argument("--reason", required=True, choices=sorted(VALID_FAIL_REASONS))
    bump.add_argument("--dedupe-key", "--event-id", dest="dedupe_key")

    for name in ("freeze", "unfreeze"):
        cmd = sub.add_parser(name, help=f"{name} a route pool")
        cmd.add_argument("pool")
        cmd.add_argument("--state-issue", help=f"issue storing pool state metadata; default ${STATE_ISSUE_ENV}")

    pause = sub.add_parser("pause", help="pause a route pool until a timestamp")
    pause.add_argument("pool")
    pause.add_argument("--until", required=True)
    pause.add_argument("--state-issue", help=f"issue storing pool state metadata; default ${STATE_ISSUE_ENV}")

    return parser


def main(argv: Optional[List[str]] = None, runner: Optional[MulticaRunner] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            result = show_card(args.issue, runner=runner)
        elif args.command == "bump":
            result = bump_card(args.issue, args.reason, dedupe_key=args.dedupe_key, runner=runner)
        elif args.command == "freeze":
            result = freeze_pool(args.pool, state_issue=args.state_issue, runner=runner)
        elif args.command == "unfreeze":
            result = unfreeze_pool(args.pool, state_issue=args.state_issue, runner=runner)
        elif args.command == "pause":
            result = pause_pool(args.pool, args.until, state_issue=args.state_issue, runner=runner)
        else:
            parser.error("unknown command")
    except RouteCardError as exc:
        parser.exit(2, f"card.py: error: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
