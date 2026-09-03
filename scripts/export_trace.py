#!/usr/bin/env python3
"""把 dsh 的 session.jsonl.zstd 导出成两个视角的 trace 文件。

用法:
  python scripts/export_trace.py <session_dir|session.jsonl.zstd> \
      --out-dir <dir> --id <id> [--reset <reset.json>]

产出（<out-dir>/ 下）:
  <id>.model_trace.json   模型可见视角（observation 已裁终局 reward/goal）
  <id>.raw_trace.json     环境原生视角（含 goal、reward_detail、progress 等）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def resolve_session_file(path: Path) -> Path:
    if path.is_dir():
        candidate = path / "session.jsonl.zstd"
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"{path} 下没有 session.jsonl.zstd")
    return path


def read_session(path: Path) -> list[dict]:
    raw = subprocess.check_output(
        ["zstd", "-dc", str(path)], stderr=subprocess.DEVNULL
    ).decode("utf-8", "replace")
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def text_of_message(message: dict) -> str:
    for block in message.get("content", []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            return block.get("text", "")
        if "content" in block and isinstance(block["content"], list):
            inner = [b.get("text", "") for b in block["content"] if isinstance(b, dict)]
            if inner:
                return "\n".join(inner)
    return ""


def _raw_state(raw: dict) -> dict:
    reward_detail = raw.get("reward_detail")
    return {
        "done": raw.get("done"),
        "termination_reason": raw.get("termination_reason"),
        "reward": raw.get("reward"),
        "reward_valid": raw.get("reward_valid"),
        "reward_type": reward_detail.get("reward_type") if isinstance(reward_detail, dict) else None,
        "purchase_success": reward_detail.get("purchase_success") if isinstance(reward_detail, dict) else None,
        "purchase": raw.get("purchase"),
    }


def build_traces(events: list[dict], reset_result: dict | None = None) -> tuple[dict, dict]:
    task = ""
    for event in events:
        if event.get("type") != "user/message":
            continue
        source = event.get("data", {}).get("source", {})
        if source.get("kind") == "user":
            task = text_of_message(event.get("data", {}))
            break

    model_steps = []
    raw_steps = []

    pending = None
    for event in events:
        t = event.get("type")
        data = event.get("data", {})

        if t == "tool/call":
            raw_args = data.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = raw_args
            pending = {
                "step": data.get("step"),
                "tool_name": data.get("name"),
                "tool_args": args,
            }
        elif t == "tool/result" and pending is not None:
            message = data.get("message", {})
            meta = data.get("meta") or {}
            raw = meta.get("raw") if isinstance(meta, dict) else None

            model_steps.append(
                {
                    "step": pending["step"],
                    "tool_name": pending["tool_name"],
                    "tool_args": pending["tool_args"],
                    "observation": text_of_message(message),
                }
            )
            if raw is not None:
                raw_steps.append(
                    {
                        "step": pending["step"],
                        "tool_name": pending["tool_name"],
                        "tool_args": pending["tool_args"],
                        "raw": raw,
                    }
                )
            pending = None

    terminal = None
    if raw_steps:
        for step in reversed(raw_steps):
            raw = step.get("raw") or {}
            if raw.get("done"):
                terminal = _raw_state(raw)
                break
        if terminal is None:
            terminal = _raw_state(raw_steps[-1].get("raw") or {})
    else:
        terminal = {
            "done": False, "termination_reason": None, "reward": None,
            "reward_valid": None, "reward_type": None,
            "purchase_success": None, "purchase": {},
        }

    model_trace = {
        "task": task,
        "step_count": len(model_steps),
        "steps": model_steps,
        "terminal": terminal,
    }
    raw_trace = {
        "task": task,
        "step_count": len(raw_steps),
        "reset": reset_result,
        "steps": raw_steps,
    }
    return model_trace, raw_trace


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session", help="session 目录 或 session.jsonl.zstd 文件")
    parser.add_argument("--out-dir", required=True, help="trace 输出目录")
    parser.add_argument("--id", required=True, help="trace 文件的 id 前缀")
    parser.add_argument("--reset", default=None, help="reset 原生返回 JSON 文件")
    args = parser.parse_args(argv)

    session_file = resolve_session_file(Path(args.session))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    reset_result = None
    if args.reset:
        reset_path = Path(args.reset)
        if reset_path.exists():
            payload = json.loads(reset_path.read_text(encoding="utf-8"))
            reset_result = payload.get("result") if isinstance(payload, dict) else None

    model_trace, raw_trace = build_traces(read_session(session_file), reset_result=reset_result)

    model_path = out_dir / f"{args.id}.model_trace.json"
    raw_path = out_dir / f"{args.id}.raw_trace.json"
    model_path.write_text(json.dumps(model_trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raw_path.write_text(json.dumps(raw_trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {model_path}")
    print(f"wrote {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
