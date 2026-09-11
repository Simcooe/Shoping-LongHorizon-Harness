#!/usr/bin/env python3
"""Subprocess lifecycle helpers for mea-v4 attempts."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


def terminate_and_wait(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=timeout)


def stop_all_children(timeout: float = 10.0) -> None:
    with _ACTIVE_LOCK:
        active = list(_ACTIVE)
    for proc in active:
        terminate_and_wait(proc, timeout=timeout)


def run_child(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
    try:
        stdout, _ = proc.communicate()
    except KeyboardInterrupt:
        terminate_and_wait(proc)
        raise
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc)
    return subprocess.CompletedProcess(command, proc.returncode, stdout=stdout, stderr=None)
