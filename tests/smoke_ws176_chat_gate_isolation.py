"""WS-176返工 smoke: GitHub live chat gate must not reuse a stale :8765 server."""

import os
import json
import stat
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_github_actions_gate_uses_isolated_checkout_url() -> None:
    with tempfile.TemporaryDirectory(prefix="ws176-chat-gate-") as td:
        tmp = Path(td)
        env_file = tmp / ".env.local"
        env_file.write_text(
            "\n".join(
                [
                    "DEEPSEEK_API_KEY=dummy",
                    "DB_URL=postgres://dummy",
                    "JWT_SECRET=dummy",
                ]
            ),
            encoding="utf-8",
        )
        probe = tmp / "assert_hipop_url.sh"
        probe.write_text(
            "#!/usr/bin/env bash\n"
            "test \"$HIPOP_URL\" = \"http://127.0.0.1:18888\"\n",
            encoding="utf-8",
        )
        probe.chmod(probe.stat().st_mode | stat.S_IXUSR)

        env = os.environ.copy()
        env.update(
            {
                "GITHUB_ACTIONS": "true",
                "HIPOP_URL": "http://127.0.0.1:8765",
                "HIPOP_CHAT_GATE_PORT": "18888",
                "HIPOP_ENV_FILE": str(env_file),
                "PYTHON": "python3",
                "WS154_SELFTEST": "1",
                "WS154_SMOKE_CMD": str(probe),
            }
        )
        result = subprocess.run(
            ["bash", str(REPO / "tests" / "ci_chat_e2e_gate.sh")],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GitHub Actions isolation" in result.stdout
    assert "http://127.0.0.1:18888" in result.stdout


def test_graded_gate_can_reuse_smoke_json_without_second_live_pass() -> None:
    with tempfile.TemporaryDirectory(prefix="ws176-graded-json-") as td:
        matrix = Path(td) / "chat-smoke.json"
        matrix.write_text(
            json.dumps(
                {
                    "cases": [{"name": "case", "grades": {
                        "correct_source": 1.0,
                        "correct_time_window": 1.0,
                        "real_task": 1.0,
                        "fail_closed": 1.0,
                        "overall": 1.0,
                    }}],
                    "averages": {
                        "correct_source": 1.0,
                        "correct_time_window": 1.0,
                        "real_task": 1.0,
                        "fail_closed": 1.0,
                        "overall": 1.0,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["HIPOP_GRADED_REQUIRE_SERVER"] = "1"
        result = subprocess.run(
            [
                "python3",
                str(REPO / "tests" / "smoke_graded_threshold.py"),
                "--from-json",
                str(matrix),
            ],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "source JSON" in result.stdout
    assert "within tolerance" in result.stdout


def main() -> None:
    test_github_actions_gate_uses_isolated_checkout_url()
    test_graded_gate_can_reuse_smoke_json_without_second_live_pass()
    print("2/2 passed (WS-176 chat gate isolation)")


if __name__ == "__main__":
    main()
