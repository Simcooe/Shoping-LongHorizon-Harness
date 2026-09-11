#!/usr/bin/env python3
"""Stage 1 fixture: interruption terminates and waits for the child process."""

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from mea_v4_process import run_child


def main():
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "child.pid"
        script = (
            "import os,time,pathlib; "
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); "
            "time.sleep(60)"
        )
        original = subprocess.Popen.communicate

        def interrupted(self, *args, **kwargs):
            deadline = time.time() + 5
            while not marker.exists() and time.time() < deadline:
                time.sleep(0.01)
            raise KeyboardInterrupt

        with mock.patch.object(subprocess.Popen, "communicate", interrupted):
            try:
                run_child([sys.executable, "-c", script], cwd=Path(tmp), env=dict())
                raise AssertionError("KeyboardInterrupt was not propagated")
            except KeyboardInterrupt:
                pass
        pid = int(marker.read_text())
        time.sleep(0.05)
        try:
            import os
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("child process is still alive")
    print("test_mea_v4_interrupt.py: all assertions passed")


if __name__ == "__main__":
    main()
