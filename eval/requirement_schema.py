#!/usr/bin/env python3
"""Rubric v2 统一 schema、事件模型与确定性时间线编译器。

本模块是纯逻辑层（无 LLM / 无环境依赖），供以下链路共享：

- 在线：Shopper 结构化事件 → 可信同步 → 环境账本（复用
  web_agent_site.engine.requirements.RequirementLedger，常量在此对齐）；
- 离线：历史轨迹事件重建（eval/reconstruct_events.py）；
- 动态 rubric 编译（eval/gen_rubric_v2.py）；
- Judge v2 / report v2 的校验（eval/judge.py、eval/report.py）。

三种需求视角（不得混淆）：
1. 初始公开约束：来自初始公开 Query（instruction_simple）；
2. Agent 已知有效约束：初始公开约束 + 截至某动作已收到的事件；
3. 环境真实有效目标：完整隐藏需求仍在，可信修改才改变目标。

事件类型语义：
- reveal：揭示既有（隐藏）需求，不改变环境目标；
- add：用户新增要求；
- modify：替换既有要求（supersede 旧约束）；
- conditional_accept：仅针对特定报价/候选/规格的条件接受（带 scope）；
- reject：拒绝候选/报价（使同范围旧接受失效，并可排除候选）；
- revoke：撤销此前事件（按确定性历史重放）。
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOP_ENV = ROOT / "environments" / "ShopSimulator" / "shop_env"
if str(SHOP_ENV) not in sys.path:
    sys.path.insert(0, str(SHOP_ENV))

# 与环境侧需求账本共享同一枚举，防止两侧漂移（纯模块，无重量级依赖）。
from web_agent_site.engine.requirements import (  # noqa: E402
    EVENT_CONDITIONAL_ACCEPT,
    EVENT_MODIFY,
    EVENT_REJECT,
    EVENT_REVOKE,
    EVENT_REVEAL,
    REQUIREMENT_PROTOCOL_VERSION,
    scope_matches_purchase,
)

SCHEMA_VERSION = "shopping-rubric-schema-v2"
EVENTS_SCHEMA_VERSION = "shopping-requirement-events-v1"
EVENT_ADD = "add"  # 环境侧无 add（账本以 reveal/modify 表达），eval 侧细分保留

EVENT_KINDS = {
    EVENT_REVEAL,
    EVENT_ADD,
    EVENT_MODIFY,
    EVENT_CONDITIONAL_ACCEPT,
    EVENT_REJECT,
    EVENT_REVOKE,
}

LIFECYCLE_ACTIVE = "active"
LIFECYCLE_SUPERSEDED = "superseded"
LIFECYCLE_REVOKED = "revoked"

HARDNESS_HARD = "hard"
HARDNESS_SOFT = "soft"

SOURCE_INITIAL_QUERY = "initial_query"
SOURCE_VISIBLE_PERSONA = "visible_persona"
SOURCE_SHOPPER_CLARIFICATION = "shopper_clarification"
SOURCES = {SOURCE_INITIAL_QUERY, SOURCE_VISIBLE_PERSONA, SOURCE_SHOPPER_CLARIFICATION}

EVENT_SOURCE_RUNTIME = "runtime"
EVENT_SOURCE_RETROSPECTIVE = "retrospective"

# 约束语义键（通用分类器输出；未知归类为 other:<token> 由调用方决定）。
KEY_BUDGET = "budget"
KEY_CATEGORY = "category"
KEY_BRAND = "brand"
KEY_MODEL = "model"
KEY_QUANTITY = "quantity"
KEY_COLOR = "color"
KEY_SIZE = "size"
KEY_MATERIAL = "material"
KEY_FUNCTION = "function"
KEY_AUDIENCE = "audience"
KEY_STYLE = "style"
KEY_PRICE_OFFER = "price_offer"

_KEY_PATTERNS = (
    (KEY_BUDGET, re.compile(r"预算|价格|元|块钱|块|花费|售价|价位")),
    (KEY_QUANTITY, re.compile(r"数量|根|支|个|只|件数|套装数|容量规格")),
    (KEY_CATEGORY, re.compile(r"品类|类别|商品类型|商品为|类型")),
    (KEY_BRAND, re.compile(r"品牌")),
    (KEY_MODEL, re.compile(r"型号")),
    (KEY_COLOR, re.compile(r"颜色|色")),
    (KEY_SIZE, re.compile(r"尺寸|尺码|大小|码|身高|厘米|cm")),
    (KEY_MATERIAL, re.compile(r"材质|材料|质地")),
    (KEY_AUDIENCE, re.compile(r"适用|人群|儿童|小孩|女[生士]|男[生士]|宠物|狗|猫")),
    (KEY_FUNCTION, re.compile(r"功能|调光|防水|防晒|可调|透气|护颈")),
    (KEY_STYLE, re.compile(r"风格|图案|款式|外观")),
)


def classify_requirement_key(text: str) -> str:
    """把约束描述/事件字段映射到通用语义键（不按 case 硬编码）。"""
    t = str(text or "")
    for key, pattern in _KEY_PATTERNS:
        if pattern.search(t):
            return key
    return "other"


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def file_hash(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 事件模型
# --------------------------------------------------------------------------- #

@dataclass
class RequirementEventV2:
    """统一事件模型（在线/离线共用）。

    - event_id：稳定唯一（在线由服务生成；离线按
      "<run>/<task>/<kind>/<seq>" 确定性映射），不得用 step 编号充当；
    - expected_version / result_version：基于前版本的乐观校验与结果版本；
    - source_reply_id / source_quote：来源回复引用与可定位原话；
    - scope：条件接受/拒绝的作用范围（asin/option_spec/quantity/amount/
      currency/candidate）；
    - status：applied / duplicate / conflict / version_mismatch /
      pending_confirmation / post_terminal_excluded / rejected_invalid。
    """

    event_id: str
    session: str = ""
    kind: str = ""
    requirement_key: str = ""
    old_value: object = None
    new_value: object = None
    source_reply_id: object = None
    source_quote: object = None
    conditions: list = field(default_factory=list)
    scope: dict | None = None
    references_event: str | None = None
    expected_version: int | None = None
    result_version: int | None = None
    seq: int | None = None
    status: str = "applied"
    # 排序/定位字段：不参与内容指纹；step 编号可能重复，仅与事件日志的
    # 稳定顺序号（seq）配合使用，无法唯一映射时保持 None（显式报缺证据）。
    step_index: int | None = None

    def to_dict(self) -> dict:
        return copy.deepcopy({
            "event_id": self.event_id,
            "session": self.session,
            "kind": self.kind,
            "requirement_key": self.requirement_key,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "source_reply_id": self.source_reply_id,
            "source_quote": self.source_quote,
            "conditions": self.conditions,
            "scope": self.scope,
            "references_event": self.references_event,
            "expected_version": self.expected_version,
            "result_version": self.result_version,
            "seq": self.seq,
            "status": self.status,
            "step_index": self.step_index,
        })

    @classmethod
    def from_dict(cls, d: dict) -> "RequirementEventV2":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def content_fingerprint(self) -> str:
        return fingerprint({
            "session": self.session,
            "kind": self.kind,
            "requirement_key": self.requirement_key,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "conditions": self.conditions,
            "scope": self.scope,
            "references_event": self.references_event,
        })


def validate_event(event: RequirementEventV2) -> list[str]:
    errors = []
    if not event.event_id:
        errors.append("event_id 缺失")
    if event.kind not in EVENT_KINDS:
        errors.append(f"kind 非法: {event.kind}")
    if not event.session:
        errors.append("session 缺失")
    if not event.requirement_key:
        errors.append("requirement_key 缺失")
    if event.kind == EVENT_CONDITIONAL_ACCEPT:
        scope = event.scope or {}
        if not scope.get("asin") and not scope.get("candidate"):
            errors.append("conditional_accept 缺少作用对象（asin/candidate）")
        if scope.get("amount") is None and scope.get("price") is None:
            errors.append("conditional_accept 缺少报价金额（amount/price）")
        if event.source_reply_id in (None, ""):
            errors.append("conditional_accept 缺少来源回复引用")
    if event.kind == EVENT_REVOKE and not event.references_event:
        errors.append("revoke 缺少 references_event")
    return errors


# --------------------------------------------------------------------------- #
# 约束模型与时间线编译（确定性重放，不依赖 LLM）
# --------------------------------------------------------------------------- #

@dataclass
class ConstraintV2:
    id: str
    requirement_key: str
    description: str
    hardness: str
    source: str
    source_event_id: str | None = None
    source_quote: str | None = None
    visible_from_event: str | None = None
    effective_from_event: str | None = None
    effective_until_event: str | None = None
    supersedes: str | None = None
    lifecycle_status: str = LIFECYCLE_ACTIVE
    scope: dict = field(default_factory=dict)
    conditions: list = field(default_factory=list)
    value: object = None
    source_events: list = field(default_factory=list)  # 合并后的来源事件（去重分母）

    def to_dict(self) -> dict:
        return copy.deepcopy({
            "id": self.id,
            "requirement_key": self.requirement_key,
            "description": self.description,
            "hardness": self.hardness,
            "source": self.source,
            "source_event_id": self.source_event_id,
            "source_quote": self.source_quote,
            "visible_from_event": self.visible_from_event,
            "effective_from_event": self.effective_from_event,
            "effective_until_event": self.effective_until_event,
            "supersedes": self.supersedes,
            "lifecycle_status": self.lifecycle_status,
            "scope": self.scope,
            "conditions": self.conditions,
            "value": self.value,
            "source_events": self.source_events,
        })


def base_constraints_to_v2(base_rubric: dict, task_id, benchmark_id) -> list[ConstraintV2]:
    """把 initial_query_only_v1 的 base rubric 条目转成 v2 约束（保留原 ID）。"""
    out = []
    for c in base_rubric.get("constraints") or []:
        out.append(ConstraintV2(
            id=c.get("id"),
            requirement_key=classify_requirement_key(
                f"{c.get('description')} {c.get('query_quote')}"
            ),
            description=c.get("description") or "",
            hardness=c.get("hardness") or HARDNESS_HARD,
            source=c.get("source") or SOURCE_INITIAL_QUERY,
            source_quote=c.get("query_quote"),
            value=c.get("query_quote"),
            source_events=[f"initial:{task_id}"],
        ))
    return out


class TimelineCompiler:
    """把事件序列编译成需求时间线（确定性重放）。

    规则：
    - reveal/add：新增约束（source=shopper_clarification）；同语义键+同值
      的重复澄清合并到同一约束（不增加分母），只追加来源事件；
    - modify：新增约束并 supersede 该语义键当前的 active 约束；
    - conditional_accept：不改写全局约束，记录为条件许可（带 scope）；
      初始预算约束保持 active；
    - reject：使同作用范围的生效条件许可失效；如携带 new_value（拒绝的
      具体取值），记录为排除项；
    - revoke：使被引用事件失效，并按确定性历史重放重算整个时间线 ——
      撤销旧修改不会错误覆盖较新有效修改；
    - post_terminal / evidence_unmapped 事件一律不进入时间线，保留原文供解释。

    重放实现：所有已应用事件按序保存；revoke 只修改撤销集合，随后从
    初始约束出发完整重放，保证生命周期推导是事件的纯函数（可审计、
    可重算、约束 ID 稳定）。
    """

    def __init__(self, session: str, initial_constraints: list[ConstraintV2]):
        self.session = session
        self._initial: list[ConstraintV2] = copy.deepcopy(initial_constraints)
        self.constraints: list[ConstraintV2] = copy.deepcopy(initial_constraints)
        self.permissions: list[dict] = []
        self.rejections: list[dict] = []
        self.exclusions: list[dict] = []
        self.versions: list[dict] = []
        self._applied_fingerprints: dict[str, str] = {}
        self._applied_events: list[RequirementEventV2] = []
        self._revoked_event_ids: set[str] = set()
        self.version = 0
        self._snapshot_version(activated_by=None)

    # ------------------------------------------------------------------ #

    def _snapshot_version(self, activated_by):
        self.versions.append({
            "requirement_version": self.version,
            "activated_by_event": activated_by,
            "active_constraint_ids": [
                c.id for c in self.constraints
                if c.lifecycle_status == LIFECYCLE_ACTIVE
            ],
            "active_permission_ids": [
                p["permission_id"] for p in self.permissions
                if p.get("lifecycle_status", LIFECYCLE_ACTIVE) == LIFECYCLE_ACTIVE
            ],
        })

    def _active_for_key(self, key: str) -> list[ConstraintV2]:
        return [
            c for c in self.constraints
            if c.requirement_key == key
            and c.lifecycle_status == LIFECYCLE_ACTIVE
        ]

    # ------------------------------------------------------------------ #

    def apply(self, event: RequirementEventV2) -> dict:
        """应用一条事件。返回 {applied, reason, requirement_version}。"""
        errors = validate_event(event)
        if errors:
            event.status = "rejected_invalid"
            return {"applied": False, "reason": "; ".join(errors),
                    "requirement_version": self.version}

        fp = event.content_fingerprint()
        known = self._applied_fingerprints.get(event.event_id)
        if known is not None:
            if known == fp:
                event.status = "duplicate"
                return {"applied": False, "reason": "duplicate_event",
                        "requirement_version": self.version}
            event.status = "conflict"
            return {"applied": False, "reason": "event_id_conflict",
                    "requirement_version": self.version}

        if (
            event.expected_version is not None
            and event.expected_version != self.version
        ):
            event.status = "version_mismatch"
            return {"applied": False, "reason": "version_mismatch",
                    "requirement_version": self.version}

        if event.status == "post_terminal_excluded":
            # 终止后的事件不进入时间线（保留记录供解释）。
            return {"applied": False, "reason": "post_terminal_excluded",
                    "requirement_version": self.version}
        if event.status == "evidence_unmapped":
            # 无法唯一定位证据来源：显式报缺证据，不默认生效。
            return {"applied": False, "reason": "evidence_unmapped",
                    "requirement_version": self.version}

        if event.kind == EVENT_REVOKE:
            target = event.references_event
            target_applied = any(
                e.event_id == target for e in self._applied_events
            )
            if not target_applied or target in self._revoked_event_ids:
                event.status = "rejected_invalid"
                return {"applied": False, "reason": "revoke_target_not_active",
                        "requirement_version": self.version}
            self._revoked_event_ids.add(target)
        else:
            self._applied_events.append(event)

        self._applied_fingerprints[event.event_id] = fp
        self.version += 1
        event.result_version = self.version
        if event.status == "applied" or event.kind == EVENT_REVOKE:
            event.status = "applied"
        self._rebuild()
        self._snapshot_version(activated_by=event.event_id)
        return {"applied": True, "reason": "ok",
                "requirement_version": self.version}

    # ------------------------------------------------------------------ #

    def _rebuild(self) -> None:
        """从初始约束出发，按序重放未撤销事件（生命周期纯函数推导）。"""
        self.constraints = copy.deepcopy(self._initial)
        self.permissions = []
        self.rejections = []
        self.exclusions = []
        self._cid = itertools.count(len(self.constraints) + 1)
        for ev in self._applied_events:
            if ev.event_id in self._revoked_event_ids:
                continue
            if ev.kind in (EVENT_REVEAL, EVENT_ADD):
                self._apply_reveal_or_add(ev)
            elif ev.kind == EVENT_MODIFY:
                self._apply_modify(ev)
            elif ev.kind == EVENT_CONDITIONAL_ACCEPT:
                self._apply_conditional_accept(ev)
            elif ev.kind == EVENT_REJECT:
                self._apply_reject(ev)
        # 被撤销事件产生的约束不在重放中出现；为保留时间线可追溯，
        # 在 constraints 末尾追加占位记录（生命周期=revoked）。
        for ev in self._applied_events:
            if ev.event_id not in self._revoked_event_ids:
                continue
            if ev.kind in (EVENT_REVEAL, EVENT_ADD, EVENT_MODIFY):
                self.constraints.append(ConstraintV2(
                    id=self._next_id(),
                    requirement_key=ev.requirement_key,
                    description=_describe(ev),
                    hardness=_hardness_of(ev),
                    source=SOURCE_SHOPPER_CLARIFICATION,
                    source_event_id=ev.event_id,
                    source_quote=ev.source_quote,
                    effective_from_event=ev.event_id,
                    lifecycle_status=LIFECYCLE_REVOKED,
                    value=copy.deepcopy(ev.new_value),
                    source_events=[ev.event_id],
                ))

    def _next_id(self) -> str:
        return f"c{next(self._cid):04d}"

    # ------------------------------------------------------------------ #

    def _apply_reveal_or_add(self, event: RequirementEventV2):
        # 同语义键+同值：合并到既有 active 约束（重复澄清不增加分母）。
        for c in self._active_for_key(event.requirement_key):
            if _values_equal(c.value, event.new_value):
                if event.event_id not in c.source_events:
                    c.source_events.append(event.event_id)
                return
        self.constraints.append(ConstraintV2(
            id=self._next_id(),
            requirement_key=event.requirement_key,
            description=_describe(event),
            hardness=_hardness_of(event),
            source=SOURCE_SHOPPER_CLARIFICATION,
            source_event_id=event.event_id,
            source_quote=event.source_quote,
            visible_from_event=event.event_id,
            effective_from_event=event.event_id,
            value=copy.deepcopy(event.new_value),
            source_events=[event.event_id],
        ))

    def _apply_modify(self, event: RequirementEventV2):
        replaced = []
        for c in self._active_for_key(event.requirement_key):
            c.lifecycle_status = LIFECYCLE_SUPERSEDED
            c.effective_until_event = event.event_id
            replaced.append(c.id)
        new = ConstraintV2(
            id=self._next_id(),
            requirement_key=event.requirement_key,
            description=_describe(event),
            hardness=_hardness_of(event),
            source=SOURCE_SHOPPER_CLARIFICATION,
            source_event_id=event.event_id,
            source_quote=event.source_quote,
            visible_from_event=event.event_id,
            effective_from_event=event.event_id,
            supersedes=replaced[0] if len(replaced) == 1 else replaced or None,
            value=copy.deepcopy(event.new_value),
            source_events=[event.event_id],
        )
        self.constraints.append(new)

    def _apply_conditional_accept(self, event: RequirementEventV2):
        # 单笔报价许可：不改写全局约束，只记录带 scope 的条件许可。
        self.permissions.append({
            "permission_id": f"p{len(self.permissions) + 1:04d}",
            "event_id": event.event_id,
            "requirement_key": event.requirement_key,
            "accepted_value": copy.deepcopy(event.new_value),
            "scope": copy.deepcopy(event.scope or {}),
            "conditions": copy.deepcopy(event.conditions or []),
            "source_quote": event.source_quote,
            "lifecycle_status": LIFECYCLE_ACTIVE,
        })

    def _apply_reject(self, event: RequirementEventV2):
        scope = event.scope or {}
        # 同作用范围的生效许可失效。
        for p in self.permissions:
            if p.get("lifecycle_status") != LIFECYCLE_ACTIVE:
                continue
            if scope and _scopes_overlap(p.get("scope") or {}, scope):
                p["lifecycle_status"] = "rejected"
        self.rejections.append({
            "event_id": event.event_id,
            "requirement_key": event.requirement_key,
            "scope": copy.deepcopy(scope),
            "source_quote": event.source_quote,
        })
        # 拒绝的具体取值进入排除项（候选重算用）。
        if event.new_value is not None:
            self.exclusions.append({
                "event_id": event.event_id,
                "requirement_key": event.requirement_key,
                "value": copy.deepcopy(event.new_value),
                "scope": copy.deepcopy(scope),
            })

    # ------------------------------------------------------------------ #

    def effective_set(self, include_keys: set | None = None) -> list[ConstraintV2]:
        out = [
            c for c in self.constraints
            if c.lifecycle_status == LIFECYCLE_ACTIVE
        ]
        if include_keys is not None:
            out = [c for c in out if c.requirement_key in include_keys]
        return out

    def result(self, **meta) -> dict:
        return copy.deepcopy({
            "schema_version": SCHEMA_VERSION,
            "requirement_protocol": REQUIREMENT_PROTOCOL_VERSION,
            "session": self.session,
            "requirement_version": self.version,
            "constraints": [c.to_dict() for c in self.constraints],
            "conditional_permissions": self.permissions,
            "rejections": self.rejections,
            "exclusions": self.exclusions,
            "requirement_versions": self.versions,
            **meta,
        })


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _values_equal(a, b) -> bool:
    return canonical_json(a) == canonical_json(b)


def _scopes_overlap(a: dict, b: dict) -> bool:
    for key in ("asin", "candidate"):
        if a.get(key) and b.get(key) and a[key] == b[key]:
            return True
    return False


def _describe(event: RequirementEventV2) -> str:
    value = event.new_value
    if isinstance(value, dict):
        inner = value.get("description") or canonical_json(value)
        return f"{event.requirement_key}: {inner}"
    return f"{event.requirement_key}: {value}"


def _hardness_of(event: RequirementEventV2) -> str:
    hardness = None
    if isinstance(event.new_value, dict):
        hardness = event.new_value.get("hardness")
    return hardness if hardness in (HARDNESS_HARD, HARDNESS_SOFT) else HARDNESS_HARD


# scope_matches_purchase 复用 engine.requirements 的单一实现（上方导入）。


# --------------------------------------------------------------------------- #
# 输出校验
# --------------------------------------------------------------------------- #

def validate_rubric_v2(doc: dict, expected_task_id=None) -> list[str]:
    errors = []
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 非法: {doc.get('schema_version')}")
    if expected_task_id is not None and str(doc.get("task_id")) != str(expected_task_id):
        errors.append(f"task_id 不匹配: {doc.get('task_id')} != {expected_task_id}")
    constraints = doc.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        errors.append("constraints 不是非空 list")
        return errors
    ids = [c.get("id") for c in constraints]
    if len(ids) != len(set(ids)):
        errors.append("约束 id 重复")
    for c in constraints:
        if not c.get("id"):
            errors.append("约束缺少 id")
        if c.get("hardness") not in (HARDNESS_HARD, HARDNESS_SOFT):
            errors.append(f"{c.get('id')}: hardness 非法")
        if c.get("source") not in SOURCES:
            errors.append(f"{c.get('id')}: source 非法")
        if c.get("lifecycle_status") not in (
            LIFECYCLE_ACTIVE, LIFECYCLE_SUPERSEDED, LIFECYCLE_REVOKED
        ):
            errors.append(f"{c.get('id')}: lifecycle_status 非法")
        if c.get("source") == SOURCE_INITIAL_QUERY and not c.get("source_quote"):
            errors.append(f"{c.get('id')}: initial_query 约束缺少 source_quote")
    versions = doc.get("requirement_versions")
    if not isinstance(versions, list) or not versions:
        errors.append("requirement_versions 不是非空 list")
    else:
        active_ids = {c.get("id") for c in constraints}
        for v in versions:
            for cid in v.get("active_constraint_ids") or []:
                if cid not in active_ids:
                    errors.append(f"版本 {v.get('requirement_version')} "
                                  f"引用了不存在的约束 {cid}")
    for p in doc.get("conditional_permissions") or []:
        scope = p.get("scope") or {}
        if not scope.get("asin") and not scope.get("candidate"):
            errors.append(f"{p.get('permission_id')}: 条件许可缺少作用对象")
    return errors
