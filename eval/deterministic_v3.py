#!/usr/bin/env python3
"""Offline deterministic evaluator for exported shopping traces.

The evaluator reads only ``manifest.json`` and ``traces/*.{model,raw}_trace.json``
under one exported run.  It never starts the harness/environment and never reads
rubrics, sessions, source goals, judgments, or private TaskFacts.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

EVALUATOR_VERSION = "deterministic-evaluator-v3"
TERMINAL_PROTOCOL = "terminal-protocol-v2"

SHOPPING_TOOLS = frozenset({"search", "click", "ask_shopper", "finish"})
ENVIRONMENT_ACTION_TOOLS = frozenset({"search", "click", "finish"})
CONTROL_TOOLS = frozenset({"mea_round_report"})

REPEAT_THRESHOLD = 2
NO_PROGRESS_THRESHOLD = 4
PERCENTILE_METHOD = "nearest-rank"
PERCENTILE_Q = 0.95

OUTCOME_CLASSES = {
    "gold_purchase": "success_gold",
    "valid_alternative_purchase": "success_valid_alternative",
    "partial_alternative_purchase": "partial_alternative_purchase",
    "wrong_purchase": "wrong_purchase",
    "graceful_stop": "graceful_stop",
    "early_abstain": "early_abstain",
    "repeat_loop": "repeat_loop",
    "max_steps": "max_steps",
    "reward_unverifiable": "reward_unverifiable",
}

NAVIGATION_ACTIONS = frozenset(
    {
        "back to search",
        "< prev",
        "next >",
        "description",
        "features",
        "reviews",
        "attributes",
    }
)

PURCHASE_OUTPUT_FIELDS = ("asin", "name", "price", "options", "category")
PURCHASE_REQUIRED_FIELDS = ("asin", "name", "price", "options")

BEHAVIOR_ANOMALY_CLASSES = (
    "repeated_action",
    "no_progress",
    "inferred_adjacent_repeat",
    "inferred_invalid_click",
    "inferred_invalid_buy",
    "inferred_invalid_option",
    "inferred_invalid_navigation",
    "invalid_click_unverifiable",
    "post_terminal_shopping_action",
    "post_terminal_control_call",
    "post_terminal_terminal_response",
)
INVALID_CLICK_CLASSES = frozenset(
    {
        "inferred_invalid_click",
        "inferred_invalid_buy",
        "inferred_invalid_option",
        "inferred_invalid_navigation",
    }
)


def _json_dumps(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json_file(path: Path) -> tuple[Any | None, str | None, str | None]:
    """Return ``(payload, sha256, error_message)`` without raising."""
    if not path.is_file():
        return None, None, "file does not exist"
    try:
        digest = sha256_file(path)
        return json.loads(path.read_text(encoding="utf-8")), digest, None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, None if not path.exists() else sha256_file(path), str(exc)


def task_sort_key(task_id: str) -> tuple[int, int | str, str]:
    try:
        return (0, int(task_id), task_id)
    except (TypeError, ValueError):
        return (1, str(task_id), str(task_id))


def normalize_action(value: Any) -> str:
    """Match the environment-facing guard: HTML-unescape, strip, case-fold."""
    return html.unescape(str(value or "")).strip().casefold()


def canonical_args(value: Any) -> str:
    """Canonical structural JSON used only for deterministic equality/repeats."""
    try:
        return _json_dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def nearest_rank(values: Iterable[int | float], q: float = PERCENTILE_Q) -> int | float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if not 0 < q <= 1:
        raise ValueError("nearest-rank percentile requires 0 < q <= 1")
    return ordered[math.ceil(q * len(ordered)) - 1]


def distribution_stats(values: Iterable[int | float]) -> dict[str, Any]:
    materialized = list(values)
    if not materialized:
        return {
            "evaluated_count": 0,
            "mean": None,
            "median": None,
            "p95": None,
            "p95_method": PERCENTILE_METHOD,
        }
    return {
        "evaluated_count": len(materialized),
        "mean": sum(materialized) / len(materialized),
        "median": statistics.median(materialized),
        "p95": nearest_rank(materialized),
        "p95_method": PERCENTILE_METHOD,
    }


def issue(
    code: str,
    message: str,
    *,
    severity: str = "error",
    source: str | None = None,
    index: int | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if source is not None:
        payload["source"] = source
    if index is not None:
        payload["index"] = index
    if evidence_refs:
        payload["evidence_refs"] = evidence_refs
    return payload


def anomaly(
    cls: str,
    *,
    index: int,
    step: dict[str, Any],
    basis: str,
    evidence_refs: list[str],
    source: str = "raw",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "class": cls,
        "source": source,
        "index": index,
        "step": step.get("step"),
        "tool_name": step.get("tool_name"),
        "basis": basis,
        "evidence_refs": evidence_refs,
    }
    if extra:
        payload.update(extra)
    return payload


def _step_list(
    payload: Any,
    side: str,
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        errors.append(issue(f"{side}_trace_not_object", f"{side} trace root is not an object", source=side))
        return None
    steps = payload.get("steps")
    if not isinstance(steps, list):
        errors.append(issue(f"{side}_steps_not_list", f"{side} trace steps is not a list", source=side))
        return None
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(
                issue(
                    f"{side}_step_not_object",
                    f"{side} trace step at index {index} is not an object",
                    source=side,
                    index=index,
                    evidence_refs=[f"{side}:/steps/{index}"],
                )
            )
    return steps


def _declared_count_check(
    payload: Any,
    steps: list[dict[str, Any]] | None,
    side: str,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    declared = payload.get("step_count") if isinstance(payload, dict) else None
    actual = len(steps) if steps is not None else None
    matches = declared == actual if isinstance(declared, int) and actual is not None else False
    if actual is not None and not isinstance(declared, int):
        errors.append(issue(f"{side}_step_count_invalid", f"{side} step_count is not an integer", source=side))
    elif actual is not None and not matches:
        errors.append(
            issue(
                f"{side}_step_count_mismatch",
                f"{side} declared step_count {declared!r} does not match actual length {actual}",
                source=side,
            )
        )
    return {"declared": declared, "actual": actual, "matches": matches if actual is not None else None}


def _alignment(
    model_steps: list[dict[str, Any]] | None,
    raw_steps: list[dict[str, Any]] | None,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    if model_steps is None or raw_steps is None:
        return {"ok": None, "paired_tool_events": None, "mismatches": None}
    mismatches: list[dict[str, Any]] = []
    if len(model_steps) != len(raw_steps):
        errors.append(
            issue(
                "model_raw_length_mismatch",
                f"model/raw lengths differ: {len(model_steps)} != {len(raw_steps)}",
                evidence_refs=["model:/steps", "raw:/steps"],
            )
        )
    paired = 0
    for index in range(min(len(model_steps), len(raw_steps))):
        model_step = model_steps[index]
        raw_step = raw_steps[index]
        fields: list[str] = []
        if not isinstance(model_step, dict) or not isinstance(raw_step, dict):
            fields.append("step_object")
        else:
            if model_step.get("step") != raw_step.get("step"):
                fields.append("step")
            if model_step.get("tool_name") != raw_step.get("tool_name"):
                fields.append("tool_name")
            if model_step.get("tool_args") != raw_step.get("tool_args"):
                fields.append("tool_args")
        if fields:
            mismatch = {"index": index, "fields": fields}
            mismatches.append(mismatch)
            errors.append(
                issue(
                    "model_raw_step_mismatch",
                    f"model/raw differ at index {index}: {', '.join(fields)}",
                    index=index,
                    evidence_refs=[f"model:/steps/{index}", f"raw:/steps/{index}"],
                )
            )
        else:
            paired += 1
    return {
        "ok": not mismatches and len(model_steps) == len(raw_steps),
        "paired_tool_events": paired,
        "mismatches": mismatches,
    }


def _protocol_check(model: Any, raw: Any, errors: list[dict[str, Any]]) -> dict[str, Any]:
    model_protocol = model.get("terminal_protocol") if isinstance(model, dict) else None
    raw_protocol = raw.get("terminal_protocol") if isinstance(raw, dict) else None
    if model_protocol is None:
        errors.append(
            issue(
                "model_terminal_protocol_missing",
                "model trace does not declare terminal_protocol",
                severity="warning",
                source="model",
                evidence_refs=["model:/terminal_protocol"],
            )
        )
    if raw_protocol is None:
        errors.append(
            issue(
                "raw_terminal_protocol_missing",
                "raw trace does not declare terminal_protocol",
                severity="warning",
                source="raw",
                evidence_refs=["raw:/terminal_protocol"],
            )
        )
    declared = [value for value in (model_protocol, raw_protocol) if value is not None]
    conflict = len(set(declared)) > 1 or any(value != TERMINAL_PROTOCOL for value in declared)
    if conflict:
        errors.append(
            issue(
                "terminal_protocol_conflict",
                f"declared protocol(s) {declared!r} conflict with evaluator protocol {TERMINAL_PROTOCOL}",
                evidence_refs=["model:/terminal_protocol", "raw:/terminal_protocol"],
            )
        )
    status = "conflict" if conflict else ("missing" if len(declared) < 2 else "match")
    return {
        "evaluator": TERMINAL_PROTOCOL,
        "model_declared": model_protocol,
        "raw_declared": raw_protocol,
        "status": status,
    }


def _raw_missing_check(
    raw_steps: list[dict[str, Any]] | None,
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[int]]:
    positions: list[dict[str, Any]] = []
    environment_gaps: set[int] = set()
    if raw_steps is None:
        return positions, environment_gaps
    for index, step in enumerate(raw_steps):
        if not isinstance(step, dict):
            environment_gaps.add(index)
            continue
        tool_name = step.get("tool_name")
        raw_value = step.get("raw")
        missing = not isinstance(raw_value, dict) or raw_value.get("raw_missing") is True
        if not missing:
            continue
        kind = (
            "environment_action"
            if tool_name in ENVIRONMENT_ACTION_TOOLS
            else "expected_non_environment"
            if tool_name in CONTROL_TOOLS or tool_name == "ask_shopper"
            else "other_tool"
        )
        positions.append({"index": index, "step": step.get("step"), "tool_name": tool_name, "kind": kind})
        severity = "warning" if kind == "expected_non_environment" else "error"
        errors.append(
            issue(
                "raw_missing",
                f"raw evidence is missing for {tool_name!r} at index {index} ({kind})",
                severity=severity,
                source="raw",
                index=index,
                evidence_refs=[f"raw:/steps/{index}/raw"],
            )
        )
        if kind in {"environment_action", "other_tool"}:
            environment_gaps.add(index)
    return positions, environment_gaps


def _terminal_summary_matches(model: Any, terminal_step: dict[str, Any] | None) -> bool | None:
    if not isinstance(model, dict) or not isinstance(model.get("terminal"), dict):
        return None
    model_terminal = model["terminal"]
    if terminal_step is None:
        return model_terminal.get("done") is not True
    raw = terminal_step.get("raw") if isinstance(terminal_step, dict) else None
    if not isinstance(raw, dict):
        return None
    reward_detail = raw.get("reward_detail")
    reward_type = (
        reward_detail.get("reward_type")
        if isinstance(reward_detail, dict) and reward_detail.get("reward_type") is not None
        else raw.get("termination_reason")
    )
    model_type = model_terminal.get("reward_type") or model_terminal.get("termination_reason")
    return (
        model_terminal.get("done") is True
        and model_type == reward_type
        and model_terminal.get("reward") == raw.get("reward")
        and model_terminal.get("purchase") == raw.get("purchase")
    )


def _extract_reset_state(raw_trace: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw_trace, dict):
        return None, None
    reset = raw_trace.get("reset")
    if not isinstance(reset, dict):
        return None, None
    state = reset.get("observation_state")
    if isinstance(state, dict):
        return state, "raw:/reset/observation_state"
    return None, None


def replay_pre_states(
    raw_trace: Any,
    raw_steps: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Replay public action-before state without carrying state across evidence gaps."""
    if raw_steps is None:
        return []
    state, state_ref = _extract_reset_state(raw_trace)
    reliable = state is not None
    replay: list[dict[str, Any]] = []
    for index, step in enumerate(raw_steps):
        step = step if isinstance(step, dict) else {}
        replay.append(
            {
                "state": state if reliable else None,
                "state_ref": state_ref if reliable else None,
                "reliable": reliable,
            }
        )
        tool_name = step.get("tool_name")
        raw_value = step.get("raw")
        raw_missing = not isinstance(raw_value, dict) or raw_value.get("raw_missing") is True
        post_state = raw_value.get("observation_state") if isinstance(raw_value, dict) else None
        if isinstance(post_state, dict):
            state = post_state
            state_ref = f"raw:/steps/{index}/raw/observation_state"
            reliable = True
        elif tool_name in ENVIRONMENT_ACTION_TOOLS:
            state = None
            state_ref = None
            reliable = False
        elif tool_name in CONTROL_TOOLS or tool_name == "ask_shopper":
            # These tools do not change ShopSimulator page state.
            pass
        elif raw_missing:
            # An unknown tool with no evidence may have changed the environment.
            state = None
            state_ref = None
            reliable = False
    return replay


def classify_invalid_click(value: Any, page_type: Any) -> str:
    normalized = normalize_action(value)
    if normalized == "buy now":
        return "inferred_invalid_buy"
    if page_type == "information_subpage":
        return "inferred_invalid_navigation"
    if page_type == "product_detail":
        return "inferred_invalid_navigation" if normalized in NAVIGATION_ACTIONS else "inferred_invalid_option"
    if normalized in NAVIGATION_ACTIONS:
        return "inferred_invalid_navigation"
    return "inferred_invalid_click"


def _purchase_result(raw: dict[str, Any] | None) -> tuple[bool | None, dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict) or "purchase" not in raw:
        return None, None, "purchase field is absent"
    purchase = raw.get("purchase")
    if purchase == {}:
        return False, None, None
    if not isinstance(purchase, dict):
        return None, None, "purchase receipt is not an object"
    missing = [field for field in PURCHASE_REQUIRED_FIELDS if field not in purchase]
    malformed: list[str] = []
    if not isinstance(purchase.get("asin"), str) or not purchase.get("asin", "").strip():
        malformed.append("asin")
    if not isinstance(purchase.get("name"), str) or not purchase.get("name", "").strip():
        malformed.append("name")
    if isinstance(purchase.get("price"), bool) or not isinstance(purchase.get("price"), (int, float, str)):
        malformed.append("price")
    if not isinstance(purchase.get("options"), dict):
        malformed.append("options")
    if missing or malformed:
        details = sorted(set(missing + malformed))
        return None, None, f"purchase receipt lacks valid required fields: {', '.join(details)}"
    brief = {field: purchase.get(field) for field in PURCHASE_OUTPUT_FIELDS if field in purchase}
    return True, brief, None


def _step_counts(steps: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if steps is None:
        return None
    names = [step.get("tool_name") for step in steps if isinstance(step, dict)]
    all_counts = Counter(name if isinstance(name, str) and name else "<missing_tool_name>" for name in names)
    control = Counter({name: count for name, count in all_counts.items() if name in CONTROL_TOOLS})
    other = Counter(
        {
            name: count
            for name, count in all_counts.items()
            if name not in SHOPPING_TOOLS and name not in CONTROL_TOOLS
        }
    )
    return {
        "total_tool_calls": len(names),
        "shopping_tool_calls": sum(all_counts[name] for name in SHOPPING_TOOLS),
        "environment_action_calls": sum(all_counts[name] for name in ENVIRONMENT_ACTION_TOOLS),
        "tool_counts": dict(sorted(all_counts.items())),
        "control_tool_counts": dict(sorted(control.items())),
        "other_tool_counts": dict(sorted(other.items())),
    }


def _paired_steps(
    model_steps: list[dict[str, Any]] | None,
    raw_steps: list[dict[str, Any]] | None,
    limit: int | None = None,
) -> list[dict[str, Any]] | None:
    if model_steps is None or raw_steps is None:
        return None
    stop = min(len(model_steps), len(raw_steps)) if limit is None else min(limit, len(model_steps), len(raw_steps))
    paired: list[dict[str, Any]] = []
    for index in range(stop):
        model_step, raw_step = model_steps[index], raw_steps[index]
        if not isinstance(model_step, dict) or not isinstance(raw_step, dict):
            continue
        if (
            model_step.get("step") == raw_step.get("step")
            and model_step.get("tool_name") == raw_step.get("tool_name")
            and model_step.get("tool_args") == raw_step.get("tool_args")
        ):
            paired.append(raw_step)
    return paired


def _scan_behavior(
    raw_trace: Any,
    raw_steps: list[dict[str, Any]] | None,
    terminal_index: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if raw_steps is None:
        return [], {
            "progress": {"eligible_count": 0, "evaluated_count": 0, "unknown_count": 0},
            "click_legality": {"eligible_count": 0, "evaluated_count": 0, "unknown_count": 0},
        }
    main_limit = len(raw_steps) if terminal_index is None else terminal_index + 1
    replay = replay_pre_states(raw_trace, raw_steps)
    anomalies: list[dict[str, Any]] = []
    progress_eligible = progress_evaluated = 0
    click_eligible = click_evaluated = 0

    previous_signature: tuple[Any, str] | None = None
    for index, step in enumerate(raw_steps[:main_limit]):
        if not isinstance(step, dict):
            previous_signature = None
            continue
        tool_name = step.get("tool_name")
        raw_value = step.get("raw") if isinstance(step.get("raw"), dict) else None

        if tool_name in ENVIRONMENT_ACTION_TOOLS:
            progress_eligible += 1
            progress = raw_value.get("progress") if isinstance(raw_value, dict) else None
            if isinstance(progress, dict):
                progress_evaluated += 1
                repeats = progress.get("consecutive_repeats")
                no_progress = progress.get("no_progress_steps")
                if isinstance(repeats, (int, float)) and not isinstance(repeats, bool) and repeats >= REPEAT_THRESHOLD:
                    anomalies.append(
                        anomaly(
                            "repeated_action",
                            index=index,
                            step=step,
                            basis=f"raw.progress.consecutive_repeats={repeats} >= {REPEAT_THRESHOLD}",
                            evidence_refs=[f"raw:/steps/{index}/raw/progress/consecutive_repeats"],
                        )
                    )
                if isinstance(no_progress, (int, float)) and not isinstance(no_progress, bool) and no_progress >= NO_PROGRESS_THRESHOLD:
                    anomalies.append(
                        anomaly(
                            "no_progress",
                            index=index,
                            step=step,
                            basis=f"raw.progress.no_progress_steps={no_progress} >= {NO_PROGRESS_THRESHOLD}",
                            evidence_refs=[f"raw:/steps/{index}/raw/progress/no_progress_steps"],
                        )
                    )

        signature = (tool_name, canonical_args(step.get("tool_args")))
        if previous_signature == signature:
            anomalies.append(
                anomaly(
                    "inferred_adjacent_repeat",
                    index=index,
                    step=step,
                    basis="tool_name and structurally canonicalized tool_args equal the preceding event",
                    evidence_refs=[f"raw:/steps/{index - 1}", f"raw:/steps/{index}"],
                )
            )
        previous_signature = signature

        if tool_name == "click":
            click_eligible += 1
            before = replay[index] if index < len(replay) else {"state": None, "state_ref": None}
            state = before.get("state")
            args = step.get("tool_args")
            value_present = isinstance(args, dict) and "value" in args
            actions_present = isinstance(state, dict) and "actions" in state
            actions = state.get("actions") if actions_present else None
            if isinstance(actions, list) and value_present:
                click_evaluated += 1
                normalized_actions = {normalize_action(action) for action in actions}
                value = args.get("value")
                if normalize_action(value) not in normalized_actions:
                    cls = classify_invalid_click(value, state.get("page_type"))
                    anomalies.append(
                        anomaly(
                            cls,
                            index=index,
                            step=step,
                            basis="normalized click value is absent from the known action-before actions",
                            evidence_refs=[f"raw:/steps/{index}/tool_args", before["state_ref"]],
                            extra={
                                "legality": "invalid",
                                "page_type": state.get("page_type"),
                                "normalized_value": normalize_action(value),
                                "available_action_count": len(actions),
                                "classification_note": (
                                    "product_detail non-navigation invalid clicks are heuristically classified as options"
                                    if cls == "inferred_invalid_option"
                                    else None
                                ),
                            },
                        )
                    )
            else:
                reason = (
                    "action-before state unavailable"
                    if state is None
                    else "action-before actions missing or malformed"
                    if not isinstance(actions, list)
                    else "click value missing"
                )
                refs = [f"raw:/steps/{index}/tool_args"]
                if before.get("state_ref"):
                    refs.append(before["state_ref"])
                anomalies.append(
                    anomaly(
                        "invalid_click_unverifiable",
                        index=index,
                        step=step,
                        basis=reason,
                        evidence_refs=refs,
                        extra={"legality": "unknown"},
                    )
                )

    if terminal_index is not None:
        for index in range(terminal_index + 1, len(raw_steps)):
            step = raw_steps[index]
            if not isinstance(step, dict):
                continue
            tool_name = step.get("tool_name")
            if tool_name in SHOPPING_TOOLS:
                anomalies.append(
                    anomaly(
                        "post_terminal_shopping_action",
                        index=index,
                        step=step,
                        basis=f"shopping tool call occurs after first done=true at index {terminal_index}",
                        evidence_refs=[f"raw:/steps/{terminal_index}/raw/done", f"raw:/steps/{index}"],
                    )
                )
            elif tool_name in CONTROL_TOOLS:
                anomalies.append(
                    anomaly(
                        "post_terminal_control_call",
                        index=index,
                        step=step,
                        basis=f"control tool call occurs after first done=true at index {terminal_index}",
                        evidence_refs=[f"raw:/steps/{terminal_index}/raw/done", f"raw:/steps/{index}"],
                    )
                )
            raw_value = step.get("raw")
            if isinstance(raw_value, dict) and raw_value.get("done") is True:
                anomalies.append(
                    anomaly(
                        "post_terminal_terminal_response",
                        index=index,
                        step=step,
                        basis=f"another done=true response occurs after locked terminal index {terminal_index}",
                        evidence_refs=[f"raw:/steps/{terminal_index}/raw/done", f"raw:/steps/{index}/raw/done"],
                    )
                )

    return anomalies, {
        "progress": {
            "eligible_count": progress_eligible,
            "evaluated_count": progress_evaluated,
            "unknown_count": progress_eligible - progress_evaluated,
        },
        "click_legality": {
            "eligible_count": click_eligible,
            "evaluated_count": click_evaluated,
            "unknown_count": click_eligible - click_evaluated,
        },
    }


def evaluate_task(
    task_id: str,
    model_path: Path,
    raw_path: Path,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    model, model_hash, model_read_error = read_json_file(model_path)
    raw, raw_hash, raw_read_error = read_json_file(raw_path)
    if model_read_error:
        errors.append(
            issue(
                "model_file_missing" if not model_path.is_file() else "model_json_unreadable",
                model_read_error,
                source="model",
            )
        )
    if raw_read_error:
        errors.append(
            issue(
                "raw_file_missing" if not raw_path.is_file() else "raw_json_unreadable",
                raw_read_error,
                source="raw",
            )
        )

    model_steps = _step_list(model, "model", errors)
    raw_steps = _step_list(raw, "raw", errors)
    model_count = _declared_count_check(model, model_steps, "model", errors)
    raw_count = _declared_count_check(raw, raw_steps, "raw", errors)
    alignment = _alignment(model_steps, raw_steps, errors)
    protocol = _protocol_check(model, raw, errors)
    raw_missing, environment_gaps = _raw_missing_check(raw_steps, errors)

    if model_steps is not None and raw_steps is not None and len(model_steps) > len(raw_steps):
        for index in range(len(raw_steps), len(model_steps)):
            step = model_steps[index]
            if isinstance(step, dict) and step.get("tool_name") in ENVIRONMENT_ACTION_TOOLS:
                environment_gaps.add(index)

    terminal_index: int | None = None
    terminal_step: dict[str, Any] | None = None
    if raw_steps is not None:
        for index, step in enumerate(raw_steps):
            raw_value = step.get("raw") if isinstance(step, dict) else None
            if isinstance(raw_value, dict) and raw_value.get("done") is True:
                terminal_index, terminal_step = index, step
                break

    raw_structurally_complete = (
        raw_steps is not None
        and raw_count["matches"] is True
        and not any(index >= len(raw_steps) for index in environment_gaps)
    )
    pre_terminal_gaps = {
        index for index in environment_gaps if terminal_index is None or index < terminal_index
    }
    evidence_incomplete = not raw_structurally_complete or bool(environment_gaps)
    terminal_selection_reliable = terminal_index is not None and not pre_terminal_gaps and raw_structurally_complete

    raw_terminal = terminal_step.get("raw") if isinstance(terminal_step, dict) else None
    reward_detail = raw_terminal.get("reward_detail") if isinstance(raw_terminal, dict) else None
    reward_type = (
        reward_detail.get("reward_type")
        if isinstance(reward_detail, dict) and reward_detail.get("reward_type") is not None
        else raw_terminal.get("termination_reason")
        if isinstance(raw_terminal, dict)
        else None
    )
    outcome_class = OUTCOME_CLASSES.get(reward_type) if reward_type is not None else None
    if terminal_index is not None and reward_type is not None and outcome_class is None:
        outcome_class = "unknown_terminal_type"
        errors.append(
            issue(
                "unknown_terminal_type",
                f"unknown reward/termination type is preserved: {reward_type!r}",
                severity="warning",
                source="raw",
                index=terminal_index,
                evidence_refs=[f"raw:/steps/{terminal_index}/raw/reward_detail/reward_type", f"raw:/steps/{terminal_index}/raw/termination_reason"],
            )
        )

    if terminal_index is None:
        environment_done: bool | None = None if evidence_incomplete else False
        gold_success = None if evidence_incomplete else False
        environment_task_success = None if evidence_incomplete else False
        purchase_occurred: bool | None = None if evidence_incomplete else False
        purchase_receipt = None
        purchase_error = None
        errors.append(
            issue(
                "no_terminal_observed",
                "no raw done=true terminal was observed",
                severity="warning",
                source="raw",
                evidence_refs=["raw:/steps"],
            )
        )
    else:
        environment_done = True
        # An observed terminal is reported, while an earlier evidence gap makes
        # its status as the *first actual* terminal uncertain.
        if terminal_selection_reliable:
            gold_success = reward_type == "gold_purchase"
            environment_task_success = reward_type in {"gold_purchase", "valid_alternative_purchase"}
        else:
            gold_success = None
            environment_task_success = None
            errors.append(
                issue(
                    "terminal_selection_unreliable",
                    "done=true was observed after an earlier environment evidence gap; it may not be the actual first terminal",
                    severity="warning",
                    source="raw",
                    index=terminal_index,
                    evidence_refs=[f"raw:/steps/{terminal_index}/raw/done"],
                )
            )
        purchase_occurred, purchase_receipt, purchase_error = _purchase_result(raw_terminal)
        if purchase_error:
            errors.append(
                issue(
                    "purchase_receipt_unverifiable",
                    purchase_error,
                    severity="warning",
                    source="raw",
                    index=terminal_index,
                    evidence_refs=[f"raw:/steps/{terminal_index}/raw/purchase"],
                )
            )

    model_terminal_matches = _terminal_summary_matches(model, terminal_step)
    if model_terminal_matches is False:
        errors.append(
            issue(
                "model_terminal_summary_conflict",
                "model terminal summary does not match the first raw done=true terminal selected by v2",
                severity="warning",
                evidence_refs=["model:/terminal", f"raw:/steps/{terminal_index}/raw" if terminal_index is not None else "raw:/steps"],
            )
        )

    anomalies, coverage = _scan_behavior(raw, raw_steps, terminal_index)
    paired_full = _paired_steps(model_steps, raw_steps)
    paired_through = _paired_steps(
        model_steps,
        raw_steps,
        None if terminal_index is None else terminal_index + 1,
    )
    full_counts = _step_counts(paired_full)
    through_counts = _step_counts(paired_through)

    protocol_acceptable = protocol["status"] != "conflict"
    core_integrity_passed = (
        model_read_error is None
        and raw_read_error is None
        and model_count["matches"] is True
        and raw_count["matches"] is True
        and alignment["ok"] is True
        and not any(item["kind"] != "expected_non_environment" for item in raw_missing)
    )
    # Legacy exports may omit the declaration entirely.  The evaluator still
    # records that as a warning, but only an explicit conflicting declaration
    # invalidates trace integrity.
    integrity_passed = core_integrity_passed and protocol_acceptable

    outcome = {
        "class": outcome_class,
        "environment_done": environment_done,
        "terminal_index": terminal_index,
        "terminal_step": terminal_step.get("step") if isinstance(terminal_step, dict) else None,
        "reward_type": reward_type,
        "termination_reason": raw_terminal.get("termination_reason") if isinstance(raw_terminal, dict) else None,
        "reward": raw_terminal.get("reward") if isinstance(raw_terminal, dict) else None,
        "reward_valid": raw_terminal.get("reward_valid") if isinstance(raw_terminal, dict) and isinstance(raw_terminal.get("reward_valid"), bool) else None,
        "environment_purchase_success_flag": (
            reward_detail.get("purchase_success")
            if isinstance(reward_detail, dict) and isinstance(reward_detail.get("purchase_success"), bool)
            else None
        ),
        "gold_success": gold_success,
        "environment_task_success": environment_task_success,
        "purchase_occurred": purchase_occurred,
        "purchase_receipt": purchase_receipt,
        "terminal_selection_reliable": terminal_selection_reliable if terminal_index is not None else None,
    }
    behavior = {
        "total_tool_calls": full_counts["total_tool_calls"] if full_counts is not None else None,
        "shopping_tool_calls": full_counts["shopping_tool_calls"] if full_counts is not None else None,
        "environment_action_calls": full_counts["environment_action_calls"] if full_counts is not None else None,
        "tool_counts": full_counts["tool_counts"] if full_counts is not None else None,
        "control_tool_counts": full_counts["control_tool_counts"] if full_counts is not None else None,
        "other_tool_counts": full_counts["other_tool_counts"] if full_counts is not None else None,
        "full_trajectory": full_counts,
        "through_terminal": through_counts,
        "count_basis": "position-aligned model/raw events; unmatched positions are excluded",
        "anomalies": anomalies,
    }
    return {
        "task_id": str(task_id),
        "evaluator_version": EVALUATOR_VERSION,
        "terminal_protocol": TERMINAL_PROTOCOL,
        "input_files": {
            "model": {"path": f"traces/{model_path.name}", "sha256": model_hash},
            "raw": {"path": f"traces/{raw_path.name}", "sha256": raw_hash},
        },
        "trace_integrity": {
            "passed": integrity_passed,
            "core_passed": core_integrity_passed,
            "model_file_readable": model_read_error is None,
            "raw_file_readable": raw_read_error is None,
            "model_step_count": model_count,
            "raw_step_count": raw_count,
            "alignment": alignment,
            "raw_missing_positions": raw_missing,
            "protocol": protocol,
            "model_terminal_matches_first_raw_terminal": model_terminal_matches,
            "environment_evidence_complete": not evidence_incomplete,
        },
        "outcome": outcome,
        "behavior": behavior,
        "coverage": coverage,
        "errors": errors,
    }


def _metric(results: list[dict[str, Any]], field: str, total: int) -> dict[str, Any]:
    values = [result["outcome"].get(field) for result in results]
    count = sum(value is True for value in values)
    unknown = sum(value is None for value in values)
    return {
        "count": count,
        "rate": count / total if total else None,
        "unknown_count": unknown,
        "denominator": total,
    }


def _event_coverage(results: list[dict[str, Any]], key: str) -> dict[str, Any]:
    eligible = sum(result["coverage"][key]["eligible_count"] for result in results)
    evaluated = sum(result["coverage"][key]["evaluated_count"] for result in results)
    unknown = eligible - evaluated
    task_eligible = sum(result["coverage"][key]["eligible_count"] > 0 for result in results)
    task_evaluated = sum(result["coverage"][key]["evaluated_count"] > 0 for result in results)
    return {
        "eligible_count": eligible,
        "evaluated_count": evaluated,
        "unknown_count": unknown,
        "rate": evaluated / eligible if eligible else None,
        "task_eligible_count": task_eligible,
        "task_evaluated_count": task_evaluated,
    }


def _anomaly_summary(results: list[dict[str, Any]], classes: set[str] | frozenset[str]) -> dict[str, Any]:
    total = len(results)
    occurrences: set[tuple[str, int]] = set()
    tasks: set[str] = set()
    for result in results:
        task_id = result["task_id"]
        for item in result["behavior"]["anomalies"]:
            if item["class"] in classes:
                occurrences.add((task_id, item["index"]))
                tasks.add(task_id)
    return {
        "task_count": len(tasks),
        "occurrence_count": len(occurrences),
        "task_rate": len(tasks) / total if total else None,
        "denominator": total,
    }


def _tool_summary(results: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    all_counts: Counter[str] = Counter()
    shopping_values: list[int] = []
    environment_values: list[int] = []
    total_values: list[int] = []
    unknown = 0
    for result in results:
        counts = result["behavior"].get(scope)
        if not isinstance(counts, dict):
            unknown += 1
            continue
        total_values.append(counts["total_tool_calls"])
        shopping_values.append(counts["shopping_tool_calls"])
        environment_values.append(counts["environment_action_calls"])
        all_counts.update(counts["tool_counts"])
    return {
        "eligible_count": len(results),
        "evaluated_count": len(results) - unknown,
        "unknown_count": unknown,
        "total_tool_calls": distribution_stats(total_values),
        "shopping_tool_calls": distribution_stats(shopping_values),
        "environment_action_calls": distribution_stats(environment_values),
        "tool_occurrence_totals": dict(sorted(all_counts.items())),
    }


def build_summary(
    results: list[dict[str, Any]],
    manifest_info: dict[str, Any],
    input_hashes: dict[str, str | None],
) -> dict[str, Any]:
    total = len(results)
    outcome_distribution = Counter(
        result["outcome"]["class"]
        if result["outcome"]["class"] is not None
        else "no_terminal_observed"
        for result in results
    )
    anomalies = {
        cls: _anomaly_summary(results, {cls})
        for cls in BEHAVIOR_ANOMALY_CLASSES
    }
    anomalies["inferred_invalid_click_all"] = _anomaly_summary(results, INVALID_CLICK_CLASSES)
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "terminal_protocol": TERMINAL_PROTOCOL,
        "thresholds": {
            "repeated_action_consecutive_repeats_gte": REPEAT_THRESHOLD,
            "no_progress_steps_gte": NO_PROGRESS_THRESHOLD,
            "p95_method": PERCENTILE_METHOD,
            "p95_quantile": PERCENTILE_Q,
        },
        "scope": {
            "read": ["manifest.json", "traces/*.model_trace.json", "traces/*.raw_trace.json"],
            "excluded": ["rubrics", "sessions", "source_goals", "judgments", "summaries", "TaskFacts"],
        },
        "inputs": {
            **manifest_info,
            "actual_readable_task_count": sum(
                result["trace_integrity"]["model_file_readable"]
                and result["trace_integrity"]["raw_file_readable"]
                for result in results
            ),
            "integrity_passed_task_count": sum(result["trace_integrity"]["passed"] for result in results),
            "core_integrity_passed_task_count": sum(result["trace_integrity"]["core_passed"] for result in results),
            "input_sha256": dict(sorted(input_hashes.items())),
        },
        "metrics": {
            "gold_success": _metric(results, "gold_success", total),
            "environment_task_success": _metric(results, "environment_task_success", total),
            "environment_terminal": _metric(results, "environment_done", total),
            "purchase_occurred": _metric(results, "purchase_occurred", total),
        },
        "outcome_classes": dict(sorted(outcome_distribution.items())),
        "tools": {
            "definition": {
                "shopping_tools": sorted(SHOPPING_TOOLS),
                "environment_action_tools": sorted(ENVIRONMENT_ACTION_TOOLS),
                "control_tools": sorted(CONTROL_TOOLS),
            },
            "full_trajectory": _tool_summary(results, "full_trajectory"),
            "through_terminal": _tool_summary(results, "through_terminal"),
        },
        "behavior_anomalies": anomalies,
        "coverage": {
            "progress_available": _event_coverage(results, "progress"),
            "click_legality_checkable": _event_coverage(results, "click_legality"),
        },
        "notes": [
            "Rates for primary outcome metrics use the full manifest task set; unknown tasks remain in the denominator.",
            "Environment reward classes are the environment's deterministic classification, not a semantic judge of the public user request.",
            "Tool counts are event counts, not token, latency, or monetary-cost estimates.",
            "purchase_occurred requires a structurally valid environment receipt and is independent of purchase correctness.",
        ],
    }


def build_failure_breakdown(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)

    integrity_tasks: dict[str, set[str]] = defaultdict(set)
    integrity_occurrences: Counter[str] = Counter()
    for result in results:
        seen: set[str] = set()
        for err in result["errors"]:
            code = err["code"]
            integrity_occurrences[code] += 1
            seen.add(code)
        for code in seen:
            integrity_tasks[code].add(result["task_id"])

    def stat(task_count: int, occurrence_count: int) -> dict[str, Any]:
        return {
            "task_count": task_count,
            "occurrence_count": occurrence_count,
            "task_rate": task_count / total if total else None,
        }

    integrity = {
        code: stat(len(integrity_tasks[code]), integrity_occurrences[code])
        for code in sorted(integrity_occurrences)
    }
    outcomes: dict[str, dict[str, Any]] = {}
    classes = Counter(
        result["outcome"]["class"] or "no_terminal_observed" for result in results
    )
    for cls, count in sorted(classes.items()):
        outcomes[cls] = stat(count, count)

    behavior = {
        cls: _anomaly_summary(results, {cls})
        for cls in BEHAVIOR_ANOMALY_CLASSES
    }
    behavior["inferred_invalid_click_all"] = _anomaly_summary(results, INVALID_CLICK_CLASSES)
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "terminal_protocol": TERMINAL_PROTOCOL,
        "manifest_task_denominator": total,
        "input_integrity_issues": integrity,
        "environment_outcomes": outcomes,
        "behavior_anomalies": behavior,
        "note": "Sections are independent, non-exclusive views; occurrence_count is the number of observed trigger positions.",
    }


def extract_manifest_ids(manifest: Any) -> tuple[list[str], dict[str, Any], list[str]]:
    fatal: list[str] = []
    if not isinstance(manifest, dict):
        return [], {"manifest_declared_task_count": None, "manifest_goal_count": 0, "manifest_unique_task_count": 0, "manifest_duplicate_task_ids": []}, ["manifest root is not an object"]
    goals = manifest.get("goals")
    if not isinstance(goals, list):
        return [], {"manifest_declared_task_count": manifest.get("task_count"), "manifest_goal_count": 0, "manifest_unique_task_count": 0, "manifest_duplicate_task_ids": []}, ["manifest goals is not a list"]
    ids: list[str] = []
    for index, goal in enumerate(goals):
        if not isinstance(goal, dict) or goal.get("task_id") is None:
            fatal.append(f"manifest goal at index {index} has no task_id")
            continue
        ids.append(str(goal["task_id"]))
    counts = Counter(ids)
    duplicates = sorted((task_id for task_id, count in counts.items() if count > 1), key=task_sort_key)
    unique_ids = sorted(counts, key=task_sort_key)
    info = {
        "manifest_declared_task_count": manifest.get("task_count"),
        "manifest_goal_count": len(goals),
        "manifest_unique_task_count": len(unique_ids),
        "manifest_duplicate_task_ids": duplicates,
    }
    if duplicates:
        fatal.append(f"manifest contains duplicate task IDs: {duplicates}")
    if isinstance(manifest.get("task_count"), int) and manifest["task_count"] != len(goals):
        fatal.append(
            f"manifest task_count {manifest['task_count']} does not match goals length {len(goals)}"
        )
    return unique_ids, info, fatal


def trace_inventory(traces_dir: Path) -> tuple[set[str], set[str]]:
    model_ids = {path.name.removesuffix(".model_trace.json") for path in traces_dir.glob("*.model_trace.json")}
    raw_ids = {path.name.removesuffix(".raw_trace.json") for path in traces_dir.glob("*.raw_trace.json")}
    return model_ids, raw_ids


def _readme() -> str:
    return f"""# Deterministic evaluation v3

This directory was produced offline by `{EVALUATOR_VERSION}` using
`{TERMINAL_PROTOCOL}`. The evaluator read only `manifest.json` and the exported
model/raw traces. It did not run DSH, ShopSimulator, or an LLM, and did not read
rubrics, sessions, source goals, judgments, summaries, or private TaskFacts.

## Semantics

- The first raw `done is true` response is the terminal and cannot be replaced.
- `gold_success` is only `gold_purchase`; `environment_task_success` additionally
  accepts `valid_alternative_purchase`.
- `purchase_occurred` requires a valid receipt (`asin`, `name`, `price`, and
  `options`); it does not mean the purchase was correct.
- Shopping tools are `search`, `click`, `ask_shopper`, and `finish`. Environment
  action calls are only `search`, `click`, and `finish`; `ask_shopper` does not
  advance ShopSimulator. `mea_round_report` is a control tool.
- Full-trajectory counts and counts through the first terminal (inclusive) are
  reported separately. P95 uses nearest-rank (`ceil(0.95*n)-1` zero-based).
- Action-before state starts at `reset.observation_state` and advances only on a
  new raw `observation_state`. `ask_shopper` and control tools retain the page.
  Missing environment evidence invalidates the page until a new state appears.
- Missing `actions` means click legality is unknown; an explicit empty list is a
  known empty set.
- Primary outcome rates use every unique manifest task as the denominator.

`summary.json` records evaluator thresholds and SHA-256 hashes for every input.
`task_results.jsonl` contains per-task evidence references. The environment
reward class is an environment metric, not a semantic judge of the user's public
request.
"""


def evaluate_run(input_dir: Path, out_dir: Path) -> tuple[dict[str, Any], int]:
    manifest_path = input_dir / "manifest.json"
    traces_dir = input_dir / "traces"
    manifest, manifest_hash, manifest_error = read_json_file(manifest_path)
    if manifest_error:
        raise ValueError(f"cannot read manifest {manifest_path}: {manifest_error}")
    if not traces_dir.is_dir():
        raise ValueError(f"traces directory does not exist: {traces_dir}")

    task_ids, manifest_info, manifest_fatal = extract_manifest_ids(manifest)
    model_ids, raw_ids = trace_inventory(traces_dir)
    expected = set(task_ids)
    missing_model = sorted(expected - model_ids, key=task_sort_key)
    missing_raw = sorted(expected - raw_ids, key=task_sort_key)
    extra_model = sorted(model_ids - expected, key=task_sort_key)
    extra_raw = sorted(raw_ids - expected, key=task_sort_key)
    manifest_info.update(
        {
            "missing_model_trace_ids": missing_model,
            "missing_raw_trace_ids": missing_raw,
            "extra_model_trace_ids": extra_model,
            "extra_raw_trace_ids": extra_raw,
        }
    )

    results = [
        evaluate_task(
            task_id,
            traces_dir / f"{task_id}.model_trace.json",
            traces_dir / f"{task_id}.raw_trace.json",
        )
        for task_id in task_ids
    ]

    input_hashes: dict[str, str | None] = {"manifest.json": manifest_hash}
    for result in results:
        for side in ("model", "raw"):
            record = result["input_files"][side]
            input_hashes[record["path"]] = record["sha256"]
    # Extra inputs are still hashed and reported, although they are not evaluated
    # as manifest tasks.
    for task_id in extra_model:
        path = traces_dir / f"{task_id}.model_trace.json"
        input_hashes[f"traces/{path.name}"] = sha256_file(path)
    for task_id in extra_raw:
        path = traces_dir / f"{task_id}.raw_trace.json"
        input_hashes[f"traces/{path.name}"] = sha256_file(path)

    summary = build_summary(results, manifest_info, input_hashes)
    summary["inputs"]["manifest_errors"] = manifest_fatal
    failure = build_failure_breakdown(results)
    failure["run_input_issues"] = {
        "manifest_errors": manifest_fatal,
        "missing_model_trace_ids": missing_model,
        "missing_raw_trace_ids": missing_raw,
        "extra_model_trace_ids": extra_model,
        "extra_raw_trace_ids": extra_raw,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    task_text = "".join(_json_dumps(result) + "\n" for result in results)
    (out_dir / "task_results.jsonl").write_text(task_text, encoding="utf-8")
    (out_dir / "summary.json").write_text(_json_dumps(summary, pretty=True) + "\n", encoding="utf-8")
    (out_dir / "failure_breakdown.json").write_text(_json_dumps(failure, pretty=True) + "\n", encoding="utf-8")
    (out_dir / "README.md").write_text(_readme(), encoding="utf-8")

    corrupt = any(
        any(error["code"] in {"model_json_unreadable", "raw_json_unreadable"} for error in result["errors"])
        for result in results
    )
    fatal_integrity = bool(
        manifest_fatal or missing_model or missing_raw or extra_model or extra_raw or corrupt
    )
    return summary, 2 if fatal_integrity else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", default="evaluations/h0", help="run directory containing manifest.json and traces/")
    parser.add_argument("--out", default=None, help="output directory (default: <input>/deterministic)")
    args = parser.parse_args(argv)
    input_dir = Path(args.input).resolve()
    out_dir = Path(args.out).resolve() if args.out else input_dir / "deterministic"
    try:
        summary, exit_code = evaluate_run(input_dir, out_dir)
    except (OSError, ValueError) as exc:
        print(f"deterministic_v3: {exc}", file=sys.stderr)
        return 2
    print(
        f"wrote {out_dir} for {summary['inputs']['manifest_unique_task_count']} manifest tasks "
        f"(exit={exit_code})"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
