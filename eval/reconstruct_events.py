#!/usr/bin/env python3
"""历史轨迹事件重建（retrospective）与新 run 事件落盘入口。

从已有 model_trace 的 ask_shopper 问答 + 初始公开输入重建结构化需求事件，
供 rubric v2 时间线编译使用。机械信息（步序、终局边界）用代码确定；
语义提取默认使用保守启发式（明确标注版本），也可加载外部（如 LLM）
产出的事件文件。**重建结果必须标注 event_source=retrospective**，不得
声称当时环境已采用这些需求或 Agent 已在新协议下重跑。

隔离要求：不读取 gold ASIN / raw reward / 隐藏完整需求来补充「用户说过」
的要求；只允许读取终局边界与事件元数据完成机械分段。

用法:
  python3 eval/reconstruct_events.py \
    --benchmark benchmarks/shopping-final-v1 \
    --traces runs/h0-0905-1446/traces \
    --run-id h0-0905-1446 \
    --out evaluations/shopping-final-v1/h0-0905-1446/events \
    [--only 916,1092] [--events-from <dir>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.requirement_schema import (  # noqa: E402
    EVENT_ADD,
    EVENT_CONDITIONAL_ACCEPT,
    EVENT_MODIFY,
    EVENT_REJECT,
    EVENT_SOURCE_RETROSPECTIVE,
    EVENTS_SCHEMA_VERSION,
    KEY_BUDGET,
    RequirementEventV2,
    canonical_json,
    classify_requirement_key,
    fingerprint,
)
from eval.trace_utils import first_terminal_step, read_json  # noqa: E402

HEURISTIC_EXTRACTOR = {"name": "heuristic", "version": "heuristic-v1"}
MOCK_EXTRACTOR = {"name": "mock-file", "version": "mock-file-v1"}

_BUDGET_IN_REPLY = re.compile(
    r"(?:预算|价格|售价|价位)?(?:就|在|是|改成|调到|提到)?"
    r"(\d+(?:\.\d+)?)\s*(万|千)?\s*(?:块钱|块|元)\s*(以内|以下|内|左右|上下)?"
)
_REJECT_WORDS = re.compile(r"不要|拒绝|不喜欢|不行|不合适|换一个|不考虑")
_CONDITIONAL_WORDS = re.compile(r"如果|确认|的话|前提是|只要")


def extract_ask_shopper_qa(model_trace: dict) -> list[dict]:
    """提取 ask_shopper 问答。真实 model_trace 中 ask_shopper 是单步：
    tool_args.question 为问题，observation 为用户回复。步骤号可能重复：
    用出现顺序号作为稳定 QA ID（不以 step 编号充当唯一 ID）。"""
    qa = []
    for s in model_trace.get("steps") or []:
        if s.get("tool_name") != "ask_shopper":
            continue
        qa.append({
            "step": s.get("step"),
            "question": str((s.get("tool_args") or {}).get("question") or ""),
            "reply": str(s.get("observation") or ""),
        })
    for i, item in enumerate(qa, 1):
        item["qa_id"] = f"qa-{i:04d}"
    return qa


def terminal_boundary(raw_trace: dict | None) -> int | None:
    """第一次真实 done 的 0-based step 索引（机械分段，允许读取）。"""
    if not raw_trace:
        return None
    idx, _ = first_terminal_step(raw_trace.get("steps") or [])
    return idx


class HeuristicExtractor:
    """保守的确定性提取器：只产出高置信事件，其余不猜。

    - 回复中出现明确金额+货币单位 → budget 事件（值带原文引用）；
    - 出现拒绝词 → reject 事件（作用范围留空，供候选重算侧保守处理）；
    - 金额+条件词 → 标记条件接受候选，但启发式无法绑定 asin/规格，
      置 pending_confirmation，不自动生效。
    """

    name = "heuristic"
    version = "heuristic-v1"

    def __init__(self, base_budget_value=None):
        self.base_budget_value = base_budget_value

    def extract(self, qa_item: dict, session: str, run_id: str,
                task_id) -> list[RequirementEventV2]:
        events = []
        reply = qa_item.get("reply") or ""
        question = qa_item.get("question") or ""
        qa_id = qa_item["qa_id"]
        base = f"{run_id}/{task_id}"

        m = _BUDGET_IN_REPLY.search(reply)
        if m:
            value = float(m.group(1))
            if m.group(2) == "万":
                value *= 10000
            elif m.group(2) == "千":
                value *= 1000
            qualifier = m.group(3)
            upper = value * 1.1 if qualifier in ("左右", "上下") else value
            budget_value = {"upper": upper, "quote": m.group(0)}
            kind = EVENT_MODIFY if (
                self.base_budget_value is not None
                and canonical_json(self.base_budget_value) != canonical_json(budget_value)
            ) else EVENT_ADD
            if _CONDITIONAL_WORDS.search(reply):
                events.append(RequirementEventV2(
                    event_id=f"{base}/conditional_accept/{qa_id}",
                    session=session,
                    kind=EVENT_CONDITIONAL_ACCEPT,
                    requirement_key=KEY_BUDGET,
                    new_value={"amount": upper},
                    source_reply_id=qa_id,
                    source_quote=m.group(0),
                    conditions=[reply],
                    scope={"amount": upper},  # 缺 asin：校验会拒绝/待确认
                    status="pending_confirmation",
                ))
            else:
                events.append(RequirementEventV2(
                    event_id=f"{base}/budget/{qa_id}",
                    session=session,
                    kind=kind,
                    requirement_key=KEY_BUDGET,
                    new_value=budget_value,
                    source_reply_id=qa_id,
                    source_quote=m.group(0),
                ))
        if _REJECT_WORDS.search(reply):
            events.append(RequirementEventV2(
                event_id=f"{base}/reject/{qa_id}",
                session=session,
                kind=EVENT_REJECT,
                requirement_key="candidate",
                new_value=question[:120],
                source_reply_id=qa_id,
                source_quote=reply[:120],
                scope={"candidate": None},
            ))
        return events


def load_mock_events(path: Path, session: str) -> list[RequirementEventV2]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    events = []
    for d in doc.get("events") or []:
        d.setdefault("session", session)
        events.append(RequirementEventV2.from_dict(d))
    return events


def base_budget_of(base_rubric_path: Path):
    """从 base rubric（公开 Query 派生，非隐藏信息）读取初始预算约束值。"""
    if not base_rubric_path.exists():
        return None
    try:
        doc = json.loads(base_rubric_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for c in doc.get("constraints") or []:
        text = f"{c.get('description', '')} {c.get('query_quote', '')}"
        if classify_requirement_key(text) == KEY_BUDGET:
            m = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
            if m:
                return {"upper": float(m.group(1))}
    return None


def reconstruct_task(task_id, model_trace: dict, raw_trace: dict | None,
                     run_id: str, benchmark_id: str, extractor,
                     events_meta: dict) -> dict:
    session = f"{run_id}/{task_id}"
    qa = extract_ask_shopper_qa(model_trace)
    boundary = terminal_boundary(raw_trace)

    # 步号可能重复且与数组位置不一一对应：用「trace 内出现顺序」映射。
    step_order = []
    seen_positions = {}
    for pos, s in enumerate(model_trace.get("steps") or []):
        step_order.append(s.get("step"))
        seen_positions.setdefault(s.get("step"), pos)

    # ask_shopper 的 step 号映射到第一次出现的数组位置（确定性映射，
    # 无法唯一映射时显式记录而不是乱猜）。
    qa_positions = []
    used_positions = set()
    for item in qa:
        pos = None
        for p, st in enumerate(model_trace.get("steps") or []):
            if p in used_positions:
                continue
            if st.get("tool_name") == "ask_shopper" and st.get("step") == item["step"]:
                pos = p
                break
        if pos is None:
            item["position_mapped"] = False
        else:
            used_positions.add(pos)
            item["position_mapped"] = True
        item["position"] = pos
        qa_positions.append(pos)

    events = []
    if hasattr(extractor, "extract"):
        for item in qa:
            events.extend(extractor.extract(item, session, run_id, task_id))
    # 外部事件（mock/LLM 产物）：补充 session 与 step 映射。
    for ev in events_meta.get("events", []):
        ev.setdefault("session", session)
        events.append(RequirementEventV2.from_dict(ev))

    # post_terminal 标注：事件来源回复位于第一次终局之后 → 排除。
    qa_by_id = {item["qa_id"]: item for item in qa}
    for ev in events:
        src = qa_by_id.get(ev.source_reply_id)
        if src is not None and ev.step_index is None:
            ev.step_index = src.get("position")
        if boundary is not None and src and src.get("position") is not None:
            if src["position"] > boundary and ev.status == "applied":
                ev.status = "post_terminal_excluded"
        # 无法定位的事件（缺证据）：显式标记，不默认生效。
        if ev.status == "applied" and ev.step_index is None:
            ev.status = "evidence_unmapped"

    for i, ev in enumerate(events, 1):
        ev.seq = i

    return {
        "schema_version": EVENTS_SCHEMA_VERSION,
        "event_source": EVENT_SOURCE_RETROSPECTIVE,
        "benchmark_id": benchmark_id,
        "run_id": run_id,
        "task_id": str(task_id),
        "trial_id": events_meta.get("trial_id", "t0"),
        "terminal_boundary": {
            "first_terminal_position": boundary,
            "protocol": "terminal-protocol-v2",
        },
        "ask_shopper_qa": [
            {
                "qa_id": item["qa_id"],
                "step": item["step"],
                "position": item.get("position"),
                "position_mapped": item.get("position_mapped", True),
                "question": item["question"],
                "reply": item["reply"],
                "post_terminal": bool(
                    boundary is not None
                    and item.get("position") is not None
                    and item["position"] > boundary
                ),
            }
            for item in qa
        ],
        "events": [ev.to_dict() for ev in events],
        "step_id_map": {
            "note": "step 编号可能重复；position 为 trace 内出现顺序，供追溯。",
            "positions": step_order,
        },
        "extractor": (
            {"name": extractor.name, "version": extractor.version}
            if hasattr(extractor, "extract") else events_meta.get("extractor", MOCK_EXTRACTOR)
        ),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="需求事件重建/装载（retrospective）")
    ap.add_argument("--benchmark", default="benchmarks/shopping-final-v1")
    ap.add_argument("--traces", required=True, help="trace 目录（*.model_trace.json）")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None, help="逗号分隔 task_id 子集")
    ap.add_argument("--events-from", default=None,
                    help="外部事件目录（<task_id>.events.json，mock/LLM 产物）")
    args = ap.parse_args(argv)

    bench = Path(args.benchmark)
    manifest = json.loads((bench / "manifest.json").read_text(encoding="utf-8"))
    benchmark_id = manifest.get("benchmark_id")
    all_ids = [str(t) for t in manifest["task_ids"]]
    ids = ([t.strip() for t in args.only.split(",") if t.strip()]
           if args.only else all_ids)

    traces_dir = Path(args.traces)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    extractor = HeuristicExtractor()
    summary_rows = []
    failed = []
    for tid in ids:
        mp = traces_dir / f"{tid}.model_trace.json"
        rp = traces_dir / f"{tid}.raw_trace.json"
        if not mp.exists():
            failed.append({"task_id": tid, "reason": "model_trace 缺失"})
            continue
        model_trace = read_json(mp)
        raw_trace = read_json(rp) if rp.exists() else None

        events_meta = {}
        if args.events_from:
            mock_path = Path(args.events_from) / f"{tid}.events.json"
            if mock_path.exists():
                doc = json.loads(mock_path.read_text(encoding="utf-8"))
                events_meta = doc
                extractor_for_task = None  # 外部事件模式
            else:
                extractor_for_task = extractor
        else:
            extractor_for_task = extractor

        if extractor_for_task is None:
            session = f"{args.run_id}/{tid}"
            ext_events = load_mock_events(
                Path(args.events_from) / f"{tid}.events.json", session
            )
            events_meta = {
                "events": [e.to_dict() for e in ext_events],
                "extractor": doc.get("extractor", MOCK_EXTRACTOR),
            }
            if "terminal_boundary" in doc:
                events_meta["terminal_boundary"] = doc["terminal_boundary"]
            result = reconstruct_task(
                tid, model_trace, raw_trace, args.run_id, benchmark_id,
                _ExternalExtractor(events_meta), events_meta,
            )
            if "terminal_boundary" in events_meta:
                result["terminal_boundary"] = events_meta["terminal_boundary"]
        else:
            base_budget = base_budget_of(bench / "rubrics" / f"{tid}.json")
            extractor_for_task = HeuristicExtractor(base_budget_value=base_budget)
            result = reconstruct_task(
                tid, model_trace, raw_trace, args.run_id, benchmark_id,
                extractor_for_task, {},
            )

        out_path = out_dir / f"{tid}.events.json"
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        applied = sum(1 for e in result["events"] if e["status"] == "applied")
        excluded = sum(
            1 for e in result["events"] if e["status"] == "post_terminal_excluded"
        )
        summary_rows.append({
            "task_id": tid,
            "qa_count": len(result["ask_shopper_qa"]),
            "event_count": len(result["events"]),
            "applied": applied,
            "post_terminal_excluded": excluded,
            "input_hash": hashlib.sha256(
                json.dumps({
                    "model_trace": mp.stat().st_mtime_ns,
                    "size": mp.stat().st_size,
                }).encode()
            ).hexdigest()[:16],
        })

    manifest_out = {
        "schema_version": EVENTS_SCHEMA_VERSION,
        "event_source": EVENT_SOURCE_RETROSPECTIVE,
        "benchmark_id": benchmark_id,
        "run_id": args.run_id,
        "terminal_protocol": "terminal-protocol-v2",
        "task_count": len(summary_rows),
        "failed": failed,
        "tasks": summary_rows,
        "note": "retrospective 重建：不代表当时环境已采用这些需求，也不代表 "
                "Agent 已在新协议下重跑。",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "tasks": len(summary_rows),
        "failed": len(failed),
        "events": sum(r["event_count"] for r in summary_rows),
        "post_terminal_excluded": sum(r["post_terminal_excluded"] for r in summary_rows),
    }, ensure_ascii=False))
    print(f"events -> {out_dir}")
    return 0


class _ExternalExtractor:
    """占位：外部事件模式下不再本地提取。"""

    name = "external"
    version = "external-v1"

    def __init__(self, events_meta):
        self._meta = events_meta

    def extract(self, qa_item, session, run_id, task_id):
        return []


if __name__ == "__main__":
    raise SystemExit(main())
