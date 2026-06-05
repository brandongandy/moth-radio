import os
import subprocess
import sys


def test_package_can_run_cli_as_module() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(part for part in ["src", env.get("PYTHONPATH", "")] if part)

    result = subprocess.run(
        [sys.executable, "-m", "scrobble_recs", "--help"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0
    assert "Local-first Last.fm recommendation tooling." in result.stdout
    assert "Commands" in result.stdout
