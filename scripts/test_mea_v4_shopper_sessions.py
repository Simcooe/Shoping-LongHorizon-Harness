#!/usr/bin/env python3
"""Stage 1 fixture: Shopper sessions are idempotent and attempt-isolated."""

import importlib.util
import json
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent / "shopper_simulator.py"
SPEC = importlib.util.spec_from_file_location("shopper_simulator_fixture", MODULE_PATH)
simulator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(simulator)


def post(base, path, body):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read())


def main():
    with tempfile.TemporaryDirectory() as tmp:
        facts = Path(tmp)
        (facts / "1.json").write_text(json.dumps({"instruction_full": "buy red"}))
        simulator.FACTS_DIR = facts
        simulator.SESSIONS.clear()
        simulator.chat = lambda messages: f"reply-{sum(1 for m in messages if m['role'] == 'user')}"
        server = ThreadingHTTPServer(("127.0.0.1", 0), simulator.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            assert post(base, "/start", {"session": "run/1#attempt-a", "idx": 1})[1]["existing"] is False
            assert post(base, "/ask", {"session": "run/1#attempt-a", "question": "q1"})[1]["reply"] == "reply-1"
            # Repeated /start is idempotent and does not erase the first turn.
            assert post(base, "/start", {"session": "run/1#attempt-a", "idx": 1})[1]["turns"] == 1
            assert post(base, "/ask", {"session": "run/1#attempt-a", "question": "q2"})[1]["reply"] == "reply-2"
            # A second attempt starts with independent history.
            post(base, "/start", {"session": "run/1#attempt-b", "idx": 1})
            assert post(base, "/ask", {"session": "run/1#attempt-b", "question": "q"})[1]["reply"] == "reply-1"
        finally:
            server.shutdown()
            server.server_close()
    print("test_mea_v4_shopper_sessions.py: all assertions passed")


if __name__ == "__main__":
    main()
