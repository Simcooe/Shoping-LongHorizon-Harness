#!/usr/bin/env python3
"""Read-only, deterministic analysis of an existing shopping run; no model calls.

Writes derived JSON/CSV under the run directory by default. Original traces and
sessions are never changed. This is diagnostic analysis, not an LLM rubric judge.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import html
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import subprocess

ENV_TOOLS = {"search", "click", "finish"}
SUCCESS = {"gold_purchase", "valid_alternative_purchase"}
ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def norm(value):
    return html.unescape(str(value or "")).strip().casefold()


def guard_norm(value):
    # Match src/buy-guard.js, which decodes only angle brackets.
    return str(value or "").replace("&lt;", "<").replace("&gt;", ">").strip().lower()


def text_blocks(message):
    return "\n".join(b.get("text", "") for b in message.get("content", [])
                     if isinstance(b, dict) and b.get("type") == "text")


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def describe(values):
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(values), "sum": sum(values), "mean": statistics.mean(values),
            "median": statistics.median(values), "p90_nearest_rank": ordered[math.ceil(.9 * len(values)) - 1],
            "min": min(values), "max": max(values)}


def counts(values):
    return dict(collections.Counter(values))


def analyze_task(tid, run, task, rec, zstd):
    mp = run / "traces" / f"{tid}.model_trace.json"
    rp = run / "traces" / f"{tid}.raw_trace.json"
    sp = run / "sessions" / rec["session"] / "session.jsonl.zstd"
    model, raw = read_json(mp), read_json(rp)
    events = [json.loads(line) for line in subprocess.check_output(
        [zstd, "-dc", str(sp)]).decode("utf-8").splitlines() if line.strip()]
    call_map, results, assistant, ends = {}, [], [], []
    usage = collections.Counter()
    for event in events:
        d = event.get("data", {})
        if event["type"] == "tool/call":
            if d["callId"] in call_map:
                raise ValueError(f"task {tid}: duplicate callId")
            call_map[d["callId"]] = d
        elif event["type"] == "tool/result":
            results.append(d)
        elif event["type"] == "assistant/message":
            assistant.append(d)
            usage.update({k: v for k, v in (d.get("usage") or {}).items()
                          if isinstance(v, (int, float))})
        elif event["type"] == "turn/end":
            ends.append(d)
    assert len(call_map) == len(results) == len(model["steps"]), f"task {tid}: call/result/export counts"
    assert len(raw["steps"]) == len(model["steps"]), f"task {tid}: raw/model mismatch"
    actions = []
    previous = (raw.get("reset") or {}).get("observation_state") or {}
    searches = []
    seen, opened = set(), set()
    for idx, (result, ms, rs) in enumerate(zip(results, model["steps"], raw["steps"]), 1):
        cid = result["message"]["source"]["callId"]
        call = call_map[cid]
        args = json.loads(call["arguments"]) if isinstance(call["arguments"], str) else call["arguments"]
        assert (call["name"], args) == (ms["tool_name"], ms["tool_args"]) == (rs["tool_name"], rs["tool_args"]), f"task {tid}: mismatched call {cid}"
        name, rr = call["name"], rs["raw"]
        value = norm(args.get("value"))
        available = {norm(x) for x in previous.get("actions", [])}
        invalid = name == "click" and value not in available
        is_buy = name == "click" and guard_norm(args.get("value")) == "buy now"
        selected = previous.get("selected_options") or {}
        # Replay only H1's pure preconditions against H0 observations. This is
        # exposure to a rule, never an estimate of H1 outcome improvement.
        guard = None
        if is_buy:
            if not previous:
                guard = "R0"
            elif previous.get("page_type") != "product_detail":
                guard = "R1"
            elif "buy now" not in {guard_norm(x) for x in previous.get("actions", [])}:
                guard = "R2"
            elif not selected:
                guard = "R3"
        if name == "search":
            searches.append(" ".join(norm(args.get("keywords")).split()))
        state = rr.get("observation_state") or {}
        if name in ENV_TOOLS:
            seen.update(str(p["asin"]) for p in state.get("products", []) if p.get("asin"))
            product = state.get("product") or {}
            if product.get("asin"):
                seen.add(str(product["asin"]))
            if name == "click" and str(product.get("asin", "")) == str(args.get("value", "")):
                opened.add(str(product["asin"]))
        action = {"action_index": idx, "dsh_step": ms["step"], "call_id": cid,
                  "tool_name": name, "tool_args": args, "pre_page": previous.get("page_type"),
                  "pre_product": (previous.get("product") or {}).get("asin"),
                  "pre_selected_options": selected, "invalid_click": invalid,
                  "buy_attempt": is_buy, "h1_deny_rule_on_h0_state": guard,
                  "post_page": state.get("page_type"), "environment_done": rr.get("done"),
                  "progress": rr.get("progress"),
                  "tool_error": any(b.get("isError") is True for b in result["message"].get("content", []) if isinstance(b, dict))}
        actions.append(action)
        if name in ENV_TOOLS:
            previous = state or previous
    env = [s for s in raw["steps"] if s["tool_name"] in ENV_TOOLS]
    terminal = next((s["raw"] for s in env if s["raw"].get("done")), {})
    all_terminals = [s["raw"] for s in env if s["raw"].get("done")]
    first_done_action = next((a["action_index"] for a in actions if a["environment_done"] is True), None)
    prefix = actions[:first_done_action] if first_done_action is not None else actions
    last_terminal = all_terminals[-1] if all_terminals else {}
    last_outcome = (last_terminal.get("reward_detail") or {}).get("reward_type") or "environment_not_terminated"
    last = terminal or (env[-1]["raw"] if env else {})
    rd = terminal.get("reward_detail") or {}
    outcome = rd.get("reward_type") or "environment_not_terminated"
    final_text = text_blocks(assistant[-1]["message"]) if assistant else ""
    # Flag only for human review: text classification is not a deterministic
    # semantic proof. The accompanying raw final text is saved for verification.
    claim_hint = bool(re.search(r"已.{0,12}(?:购买|下单|提交)|购买.{0,8}(?:完成|成功)|订单已提交", final_text))
    dimensions = ((rd.get("evidence") or {}).get("preference_scoring") or {}).get("dimensions") or {}
    bad_dimensions = {k: v for k, v in dimensions.items()
                      if v.get("active") and v.get("score", 0) < 1}
    p = last.get("progress") or {}
    loop_trigger = None
    if outcome == "repeat_loop":
        loop_trigger = "exact_repeat" if p.get("consecutive_repeats", 0) >= 2 else (
            "no_progress" if p.get("no_progress_steps", 0) >= 4 else "unresolved")
    goal_asin = str(task.get("private_gold_asin") or (terminal.get("goal") or {}).get("asin") or "")
    primary_seen, primary_opened = set(), set()
    for s in raw["steps"][:len(prefix)]:
        os = s["raw"].get("observation_state") or {}
        primary_seen.update(str(p["asin"]) for p in os.get("products", []) if p.get("asin"))
        product = os.get("product") or {}
        if product.get("asin"):
            primary_seen.add(str(product["asin"]))
            if s["tool_name"] == "click" and str(s["tool_args"].get("value")) == str(product["asin"]):
                primary_opened.add(str(product["asin"]))
    buy_actions = [a for a in actions if a["buy_attempt"]]
    meta = task.get("metadata") or {}
    output = {
        "task_id": tid, "query": task.get("query"), "domain": task.get("domain"),
        "complexity": meta.get("complexity"), "has_budget_metadata": meta.get("has_budget"),
        "outcome": outcome, "success": outcome in SUCCESS,
        "last_terminal_outcome": last_outcome,
        "first_done_action": first_done_action,
        "terminal_result_count": len(all_terminals),
        "terminal_outcome_changed": outcome != last_outcome,
        "post_terminal_tool_calls": len(actions) - len(prefix),
        "primary_tool_calls": len(prefix),
        "primary_ask_count": sum(a["tool_name"] == "ask_shopper" for a in prefix),
        "primary_invalid_clicks": sum(a["invalid_click"] for a in prefix),
        "environment_done": bool(terminal), "reward_valid": terminal.get("reward_valid"),
        "terminal_reward": terminal.get("reward"),
        "purchase_committed": bool(terminal.get("purchase")),
        "target_asin_match": rd.get("target_asin_match") if terminal.get("purchase") else None,
        "gold_seen": goal_asin in seen if goal_asin else None,
        "gold_opened": goal_asin in opened if goal_asin else None,
        "primary_gold_seen": goal_asin in primary_seen if goal_asin else None,
        "primary_gold_opened": goal_asin in primary_opened if goal_asin else None,
        "tool_calls": len(actions), "environment_actions": len(env),
        "llm_steps": len(assistant), "tool_counts": counts(a["tool_name"] for a in actions),
        "ask_count": sum(a["tool_name"] == "ask_shopper" for a in actions),
        "first_ask_action": next((a["action_index"] for a in actions if a["tool_name"] == "ask_shopper"), None),
        "unique_searches": len(set(searches)), "repeated_searches": len(searches) - len(set(searches)),
        "seen_product_count": len(seen), "opened_product_count": len(opened),
        "invalid_clicks": sum(a["invalid_click"] for a in actions),
        "buy_attempts": len(buy_actions),
        "h1_deny_exposures": sum(a["h1_deny_rule_on_h0_state"] is not None for a in buy_actions),
        "loop_trigger": loop_trigger,
        "failed_dimensions": bad_dimensions, "hard_gates": rd.get("hard_gates") or {},
        "price_resolution": (rd.get("evidence") or {}).get("price_resolution"),
        "purchase": terminal.get("purchase") or {}, "final_text": final_text,
        "nonterminal_purchase_claim_hint": not terminal and claim_hint,
        "agent_turn_end": ends[-1].get("reason") if ends else None,
        "repeated_dsh_step_ids": len({a["dsh_step"] for a in actions}) != len(actions),
        "agent_usage": dict(usage), "actions": actions,
        "clarifications": [{"action_index": i, "question": s["tool_args"].get("question"), "reply": s["observation"]}
                           for i, s in enumerate(model["steps"], 1) if s["tool_name"] == "ask_shopper"],
        "source_hashes": {"model_trace": fingerprint(mp), "raw_trace": fingerprint(rp), "session": fingerprint(sp)},
    }
    return output


def grouped(rows, key):
    groups = collections.defaultdict(list)
    for row in rows:
        groups[str(key(row))].append(row)
    return {k: {"n": len(v), "success": sum(r["success"] for r in v),
                "success_rate": sum(r["success"] for r in v) / len(v),
                "outcomes": counts(r["outcome"] for r in v),
                "tool_calls": describe([r["tool_calls"] for r in v])}
            for k, v in sorted(groups.items())}


def summarize(rows):
    actions = [a for r in rows for a in r["actions"]]
    buys = [a for a in actions if a["buy_attempt"]]
    invalid = [a for a in actions if a["invalid_click"]]
    partial = [r for r in rows if r["outcome"] == "partial_alternative_purchase"]
    usage = collections.Counter()
    for r in rows:
        usage.update(r["agent_usage"])
    return {
        "task_count": len(rows), "success_count": sum(r["success"] for r in rows),
        "outcomes": counts(r["outcome"] for r in rows),
        "last_terminal_outcomes": counts(r["last_terminal_outcome"] for r in rows),
        "last_terminal_success_count": sum(r["last_terminal_outcome"] in SUCCESS for r in rows),
        "post_terminal_task_ids": [r["task_id"] for r in rows if r["post_terminal_tool_calls"]],
        "post_terminal_tool_calls": sum(r["post_terminal_tool_calls"] for r in rows),
        "terminal_outcome_changes": [{"task_id": r["task_id"], "first": r["outcome"], "last": r["last_terminal_outcome"]}
                                     for r in rows if r["terminal_outcome_changed"]],
        "purchases": sum(r["purchase_committed"] for r in rows),
        "tool_counts": counts(a["tool_name"] for a in actions),
        "tool_calls": describe([r["tool_calls"] for r in rows]),
        "environment_actions": describe([r["environment_actions"] for r in rows]),
        "valid_terminal_rewards": describe([r["terminal_reward"] for r in rows if r["reward_valid"] is True]),
        "all_terminal_rewards": describe([r["terminal_reward"] for r in rows if r["environment_done"]]),
        "ask_distribution": counts(str(r["ask_count"]) for r in rows),
        "by_ask": grouped(rows, lambda r: r["primary_ask_count"] > 0),
        "by_domain": grouped(rows, lambda r: r["domain"]),
        "by_complexity": grouped(rows, lambda r: r["complexity"]),
        "by_outcome": grouped(rows, lambda r: r["outcome"]),
        "invalid_click_count": len(invalid), "invalid_click_tasks": sum(r["invalid_clicks"] > 0 for r in rows),
        "primary_invalid_click_count": sum(r["primary_invalid_clicks"] for r in rows),
        "primary_tool_calls": sum(r["primary_tool_calls"] for r in rows),
        "invalid_click_pre_pages": counts(a["pre_page"] for a in invalid),
        "invalid_click_targets": collections.Counter(str(a["tool_args"].get("value")) for a in invalid).most_common(15),
        "h1_rule_exposures": counts(a["h1_deny_rule_on_h0_state"] or "allow" for a in buys),
        "h1_exposed_tasks": [r["task_id"] for r in rows if r["h1_deny_exposures"]],
        "h1_exposure_by_outcome": counts(r["outcome"] for r in rows if r["h1_deny_exposures"]),
        "loop_triggers": counts(r["loop_trigger"] for r in rows if r["loop_trigger"]),
        "partial_target_match": counts(str(r["target_asin_match"]) for r in partial),
        "partial_failed_dimensions": counts(k for r in partial for k in r["failed_dimensions"]),
        "partial_dimension_combinations": counts('+'.join(sorted(r["failed_dimensions"])) for r in partial),
        "unverifiable_price_methods": counts((r["price_resolution"] or {}).get("method") for r in rows if r["outcome"] == "reward_unverifiable"),
        "nonterminal_ids": [r["task_id"] for r in rows if not r["environment_done"]],
        "nonterminal_purchase_claim_hint_ids": [r["task_id"] for r in rows if r["nonterminal_purchase_claim_hint"]],
        "repeated_dsh_step_tasks": sum(r["repeated_dsh_step_ids"] for r in rows),
        "tool_errors": sum(a["tool_error"] for a in actions), "agent_usage": dict(usage),
        "notes": ["Clarification and success correlations are observational, not causal.",
                  "Primary outcome is the first done=true environment result; last-terminal outcomes are retained separately.",
                  "Action counts include the entire recorded session unless explicitly prefixed primary_.",
                  "Invalid-click diagnostics compare HTML-decoded/case-normalized targets against pre-action raw observation_state.actions; these are interface-contract violations, not HTTP/tool errors.",
                  "H1 rule replay measures exposure on H0 states, not H1 success or false-block rates.",
                  "No LLM judge or external API was called. Purchase-claim regex results require human review.",
                  "Goal-based details and raw purchase fields are offline diagnostics, not Judge inputs.",
                  "Token totals include cached input; reasoningTokens is a subset of outputTokens, not additive.",
                  "Shopper and evaluator API usage is not included in agent session token usage."]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--benchmark", type=Path, default=ROOT / "benchmarks/shopping-final-v1")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    zstd = shutil.which("zstd")
    if not zstd:
        raise SystemExit("zstd executable is required")
    run, bm = args.run.resolve(), args.benchmark.resolve()
    manifest = read_json(run / "manifest.json")
    benchmark = read_json(bm / "manifest.json")
    ids = set(benchmark["task_ids"])
    tasks = {r["task_id"]: r for r in map(json.loads, (bm / "tasks.jsonl").read_text().splitlines())}
    private_path = bm / "source_goals.private.jsonl"
    if private_path.exists():
        private = {r["task_id"]: r for r in map(json.loads, private_path.read_text().splitlines())}
        assert set(private) == ids
        for tid, task in tasks.items():
            task["private_gold_asin"] = private[tid]["asin"]
    records = {r["task_id"]: r for r in manifest["goals"]}
    assert len(ids) == len(benchmark["task_ids"])
    assert len(records) == len(manifest["goals"])
    assert set(tasks) == set(records) == ids
    for suffix in ("model_trace", "raw_trace"):
        assert {int(p.name.split('.')[0]) for p in (run / "traces").glob(f"*.{suffix}.json")} == ids
    rows = [analyze_task(tid, run, tasks[tid], records[tid], zstd) for tid in sorted(ids)]
    summary = summarize(rows)
    summary.update({"run": str(run), "benchmark": str(bm),
                    "benchmark_id": benchmark["benchmark_id"], "profile": manifest["profile"],
                    "source_hashes": {"run_manifest": fingerprint(run / "manifest.json"),
                                      "benchmark_manifest": fingerprint(bm / "manifest.json"),
                                      "tasks": fingerprint(bm / "tasks.jsonl"),
                                      "analysis_script": fingerprint(Path(__file__))}})
    if private_path.exists():
        summary["source_hashes"]["private_goals"] = fingerprint(private_path)
    assert sum(summary["outcomes"].values()) == len(rows)
    assert sum(summary["tool_counts"].values()) == sum(r["tool_calls"] for r in rows)
    out = args.out or run / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (out / "tasks.jsonl").write_text(''.join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    fields = ["task_id", "domain", "complexity", "outcome", "success", "environment_done", "reward_valid",
              "terminal_reward", "purchase_committed", "target_asin_match", "tool_calls", "environment_actions",
              "llm_steps", "ask_count", "first_ask_action", "opened_product_count", "unique_searches",
              "repeated_searches", "invalid_clicks", "buy_attempts", "h1_deny_exposures", "loop_trigger",
              "nonterminal_purchase_claim_hint", "repeated_dsh_step_ids", "last_terminal_outcome",
              "first_done_action", "post_terminal_tool_calls", "primary_tool_calls", "primary_ask_count", "primary_invalid_clicks"]
    with (out / "tasks.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("by_domain", "by_complexity", "by_outcome", "source_hashes", "notes")}, ensure_ascii=False, indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
