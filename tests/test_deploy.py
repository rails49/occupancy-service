"""`scripts/deploy.sh`, with a fake `ssh` on the `PATH` that runs the box's half here."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OCCUPANCY_ENV = Path("/etc/rails49/occupancy.env")


@pytest.mark.skipif(OCCUPANCY_ENV.exists(), reason=f"{OCCUPANCY_ENV} is on this machine")
def test_a_box_with_no_occupancy_env_stops_with_a_sentence_naming_it(tmp_path: Path) -> None:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    ssh = bin_ / "ssh"
    ssh.write_text('#!/bin/sh\nexec bash -c "$2"\n')
    ssh.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OCCUPANCY_")}
    done = subprocess.run(
        [str(ROOT / "scripts" / "deploy.sh"), "user@box"],
        env=env | {"HOME": str(tmp_path), "PATH": f"{bin_}{os.pathsep}{env['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert f"this box has no {OCCUPANCY_ENV}" in done.stderr
