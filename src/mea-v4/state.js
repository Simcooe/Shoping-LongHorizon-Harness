/**
 * mea-v4 — 论文式结构化 Task State 与 Audit Reducer（阶段1）。
 *
 * 本模块只提供纯数据结构、校验器与 reducer：
 *   - createTaskState / addRequirement（shopper reply 派生）
 *   - applyAuditReport（audit reducer，唯一切换 completed 的入口）
 *   - applyDecision（仅切换决策；不接受未核验的 record 更新）
 *   - finalize（done 合法性判定）
 *   - cloneState
 *
 * 它不连接真实模型，不读写 DSH runtime，不接入 mea-loop 在线插件。
 *
 * 论文来源：reference/LongHorizon-Harness.pdf Method 2.1 / 2.2 / 2.4。
 *
 * 硬规则（与 docs/prompts/longhorizon-paper-alignment/01_STATE_AND_CONTRACT.md 一致）：
 *   1. Executor report 不能直接更新 requirement/artifact/fact。
 *   2. completed 必须引用 status=complete 且 integrity=clean 的 Audit Report。
 *   3. evidence ref 必须存在并属于对应 audit。
 *   4. suspect/violation audit 只能产生 pending/blocked/untrusted 更新。
 *   5. 新 shopper 回复可增加或修改 requirement，但必须保存原文与来源。
 *   6. done 只有在所有有效 requirement 均 completed 且没有 unresolved
 *      integrity violation 时合法。
 *
 * @module @shopping-longhorizon/shop-tools/mea-v4/state
 */

import { validateAuditReport } from './schema.js'

export const TASK_STATE_SCHEMA = 'longhorizon-task-state-v1'

const RECORD_STATUS = new Set(['pending', 'completed', 'blocked', 'untrusted'])
const DECISION_KINDS = new Set(['execute', 'done', 'blocked', 'ask'])

let seq = 0

/** 单调递增短 id 前缀，便于测试与审计引用。 */
function nextSeq(prefix) {
  seq += 1
  return `${prefix}-${seq}`
}

/** 重置全局 id 序列（仅供测试或确定性重放）。 */
export function resetSeq(value = 0) {
  seq = value
}

function asNonEmptyString(value, field) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`${field} must be a non-empty string`)
  }
  return value
}

function asStringArray(value, field, { max = 50, allowEmpty = false, maxLen = 1000 } = {}) {
  if (!Array.isArray(value) || value.length > max) {
    throw new Error(`${field} must be an array (max ${max})`)
  }
  for (const item of value) {
    if (typeof item !== 'string' || (!allowEmpty && item.length === 0) || item.length > maxLen) {
      throw new Error(`${field} items must be bounded strings`)
    }
  }
  return [...value]
}

function copy(value) {
  return value == null ? null : JSON.parse(JSON.stringify(value))
}

function makeRequirement(text, source, id) {
  return {
    id: id || nextSeq('req'),
    text: asNonEmptyString(text, 'requirement.text'),
    source: source === 'shopper_reply' ? 'shopper_reply' : 'initial_request',
    status: 'pending',
    evidence_refs: [],
  }
}

/**
 * 构造初始 Task State。initial_requirements 由调用方从公开任务 T 派生；
 * 本函数不读取任何 reward / gold / 私有 TaskFacts。
 */
export function createTaskState({
  taskId,
  originalGoal,
  initialRequirements = [],
  round = 0,
  seqStart = 0,
} = {}) {
  if (seqStart != null) resetSeq(seqStart)
  const id = asNonEmptyString(taskId, 'taskId')
  const originalGoalText = asNonEmptyString(originalGoal, 'originalGoal')
  if (!Array.isArray(initialRequirements)) {
    throw new Error('initialRequirements must be an array')
  }
  if (initialRequirements.length > 100) {
    throw new Error('initialRequirements too large (max 100)')
  }
  const requirements = initialRequirements.map((r, i) => {
    if (!r || typeof r !== 'object') throw new Error('initialRequirements items must be objects')
    return makeRequirement(r.text ?? '', r.source ?? 'initial_request', `req-${i + 1}`)
  })
  return {
    schema: TASK_STATE_SCHEMA,
    task: { id, original_goal: originalGoalText },
    requirements,
    artifacts: [],
    facts: [],
    audit_history: [],
    round: Number.isInteger(round) && round >= 0 ? round : 0,
    decision: { kind: 'execute', reason: 'initial state' },
  }
}

/** 校验证据引用是否属于某条 Audit Report。 */
function evidenceBelongsToAudit(audit, ref) {
  const evidence = Array.isArray(audit?.evidence) ? audit.evidence : []
  if (evidence.length > 0) return evidence.some(e => e && e.id === ref)
  return (audit?.evidence_ids ?? []).includes(ref)
}

/** 判断一条 Audit Report 是否"干净且完整"。 */
function isCleanCompleteAudit(audit) {
  return audit?.status === 'complete' && audit?.integrity === 'clean'
}

function checkEvidenceRefsBelongTo(audit, refs) {
  for (const ref of refs) {
    if (!evidenceBelongsToAudit(audit, ref)) {
      throw new Error(`evidence ref "${ref}" does not belong to audit "${audit.id}"`)
    }
  }
}

/**
 * 将一条 Audit Report 归并进 Task State（audit reducer，阶段1 核心）。
 *
 * 硬规则：
 *   - completed 只接受 status=complete 且 integrity=clean 的 audit；
 *   - suspect/violation audit 的 status_updates 只能落到 pending/blocked/untrusted；
 *   - 所有 evidence_refs 必须真实存在且属于本 audit。
 */
export function applyAuditReport(state, auditReport) {
  if (!state || typeof state !== 'object') throw new Error('state must be an object')
  if (state.schema !== TASK_STATE_SCHEMA) {
    throw new Error(`unsupported state schema "${state.schema}"`)
  }
  const validated = validateAuditReport(auditReport)
  const audit = { ...validated }
  const result = copy(state)

  if (result.audit_history.some(a => a.id === audit.id)) {
    throw new Error(`duplicate audit id "${audit.id}"`)
  }
  result.audit_history.push(audit)

  // 1. 显式 status_updates（引用必须属于本 audit；completed 仅限 clean+complete）。
  for (const update of audit.status_updates ?? []) {
    const list = pickRecordList(result, update.id)
    const idx = list.findIndex(r => r.id === update.id)
    if (idx === -1) {
      // fact/artifact 允许新增；requirement 新增必须走 addRequirement。
      if (update.id.startsWith('req-')) {
        throw new Error(`unknown requirement id "${update.id}"`)
      }
      const record = {
        id: update.id,
        text: '',
        status: 'pending',
        evidence_refs: [],
      }
      list.push(record)
      applyStatusUpdate(list[list.length - 1], update, audit)
      continue
    }
    applyStatusUpdate(list[idx], update, audit)
  }

  // 2. 新增 fact（审计产生的新环境事实）。
  for (const f of audit.new_facts ?? []) {
    const status = f.status ?? 'pending'
    if (status === 'completed' && !isCleanCompleteAudit(audit)) {
      throw new Error('suspect/violation audit cannot produce completed fact')
    }
    if (status === 'completed') checkEvidenceRefsBelongTo(audit, f.evidence_refs)
    result.facts.push({
      id: f.id || nextSeq('fact'),
      text: f.text,
      status,
      evidence_refs: [...(f.evidence_refs ?? [])],
    })
  }

  result.round += 1
  return result
}

function pickRecordList(state, id) {
  if (id.startsWith('req-')) return state.requirements
  if (id.startsWith('art-')) return state.artifacts
  if (id.startsWith('fact-')) return state.facts
  throw new Error(`unknown record id "${id}"`)
}

function applyStatusUpdate(record, update, audit) {
  const nextStatus = update.status
  const refs = asStringArray(update.evidence_refs ?? [], 'status_updates[].evidence_refs', { max: 50 })
  if (nextStatus === 'completed') {
    if (!isCleanCompleteAudit(audit)) {
      throw new Error('suspect/violation audit cannot produce completed')
    }
    if (refs.length === 0) throw new Error('completed requires evidence refs')
    checkEvidenceRefsBelongTo(audit, refs)
  }
  if (!RECORD_STATUS.has(nextStatus)) {
    throw new Error(`invalid record status "${nextStatus}"`)
  }
  record.status = nextStatus
  record.evidence_refs = refs
  return record
}

/**
 * 依据 shopper 原文新增或修改 requirement（不读取私有事实）。
 * 修改已有 requirement 时保留其 id/evidence_refs，仅替换 text 与 source。
 */
export function addRequirement(state, { text, source = 'shopper_reply', id } = {}) {
  if (!state || typeof state !== 'object') throw new Error('state must be an object')
  const requirementText = asNonEmptyString(text, 'requirement.text')
  const requirementSource = source === 'initial_request' ? 'initial_request' : 'shopper_reply'
  const result = copy(state)
  if (id) {
    const idx = result.requirements.findIndex(r => r.id === id)
    if (idx === -1) throw new Error(`requirement "${id}" not found`)
    result.requirements[idx] = {
      ...result.requirements[idx],
      text: requirementText,
      source: requirementSource,
    }
    return result
  }
  result.requirements.push(makeRequirement(requirementText, requirementSource, null))
  return result
}

/**
 * 应用 Manager 决策。只切换 decision，不接受任何未核验的 record 更新。
 * （record 更新只能通过 applyAuditReport / addRequirement 进入。）
 */
export function applyDecision(state, payload = {}) {
  if (!state || typeof state !== 'object') throw new Error('state must be an object')
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('decision must be an object')
  }
  const allowed = new Set(['kind', 'reason'])
  for (const key of Object.keys(payload)) {
    if (!allowed.has(key)) throw new Error(`unknown decision field "${key}"`)
  }
  const { kind, reason } = payload
  if (!DECISION_KINDS.has(kind)) throw new Error(`invalid decision kind "${kind}"`)
  if (typeof reason !== 'string' || reason.length === 0 || reason.length > 2000) {
    throw new Error('reason must be a non-empty bounded string')
  }
  const result = copy(state)
  result.decision = { kind, reason }
  return result
}

/**
 * 终局合法性：所有有效 requirement 均 completed 且无 unresolved integrity violation。
 * 返回 { ok, reason }。
 */
export function finalize(state, reason = 'all requirements completed') {
  if (!state || typeof state !== 'object') throw new Error('state must be an object')
  const openViolations = (state.audit_history ?? []).filter(a => a.integrity === 'violation')
  const effectiveRequirements = (state.requirements ?? []).filter(r => r.status !== 'blocked')
  const pending = effectiveRequirements.filter(r => r.status !== 'completed')
  if (openViolations.length > 0) {
    return { ok: false, reason: `unresolved integrity violation: ${openViolations.map(a => a.id).join(', ')}` }
  }
  if (pending.length > 0) {
    return { ok: false, reason: `incomplete requirements: ${pending.map(r => r.id).join(', ')}` }
  }
  return { ok: true, reason }
}

/** 深度拷贝，避免调用方意外共享可变引用。 */
export function cloneState(state) {
  return copy(state)
}
