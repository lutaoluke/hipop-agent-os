"""smoke_server_env_file.py — WS-128 server HIPOP_ENV_FILE loading contract.

The chat smoke runner sources HIPOP_ENV_FILE before computing DB expectations.
The uvicorn process must load the same file before importing server.data, or
test-chat can compare live DB expectations against replies from the default
local SQLite DB.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_server_main_loads_hipop_env_file():
    """Importing hipop.server.main loads HIPOP_ENV_FILE key/value pairs."""
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("WS128_ENV_PROBE=loaded-from-hipop-env-file\n")
        env_file = f.name

    env = os.environ.copy()
    env.pop("WS128_ENV_PROBE", None)
    env["HIPOP_ENV_FILE"] = env_file
    env["PYTHONPATH"] = str(REPO)

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os; "
                    "import hipop.server.main; "
                    "print(os.environ.get('WS128_ENV_PROBE', '<missing>'))"
                ),
            ],
            cwd=str(REPO),
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
    finally:
        try:
            os.unlink(env_file)
        except FileNotFoundError:
            pass

    assert proc.returncode == 0, proc.stderr[-1000:]
    assert proc.stdout.strip().splitlines()[-1] == "loaded-from-hipop-env-file", (
        "hipop.server.main did not load HIPOP_ENV_FILE before server startup; "
        f"stdout={proc.stdout!r} stderr={proc.stderr[-500:]!r}"
    )


if __name__ == "__main__":
    print("▶ smoke_server_env_file — WS-128 server env-file loading")
    tests = [
        ("test_server_main_loads_hipop_env_file", test_server_main_loads_hipop_env_file),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")
            failed += 1

    if failed:
        print(f"\n✗ {failed}/{len(tests)} tests failed")
        sys.exit(1)
    print(f"\n✓ smoke_server_env_file all {len(tests)} passed")
