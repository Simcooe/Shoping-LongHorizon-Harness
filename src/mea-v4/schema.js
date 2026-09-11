/**
 * mea-v4 — Contract 与 Audit Report 校验器（阶段1）。
 *
 * 纯校验，不读写 DSH runtime。Contract 用于约束 Executor 边界；
 * Audit Report 的 status / integrity 由 reducer 消费。
 *
 * @module @shopping-longhorizon/shop-tools/mea-v4/schema
 */

export const CONTRACT_SCHEMA = 'longhorizon-contract-v1'
export const AUDIT_SCHEMA = 'longhorizon-audit-v1'

const AUDIT_STATUS = new Set(['complete', 'incomplete', 'blocked'])
const AUDIT_INTEGRITY = new Set(['clean', 'suspect', 'violation'])
const RECORD_STATUS = new Set(['pending', 'completed', 'blocked', 'untrusted'])

const SHOP_TOOL_NAMES = new Set(['search', 'click', 'finish', 'ask_shopper'])

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

function asId(value, field) {
  return asNonEmptyString(value, field)
}

function assertAllowedKeys(obj, allowed, context) {
  for (const key of Object.keys(obj)) {
    if (!allowed.includes(key)) throw new Error(`unknown ${context} field "${key}"`)
  }
}

/** 校验 Contract 最小 schema。 */
export function validateContract(contract) {
  if (!contract || typeof contract !== 'object' || Array.isArray(contract)) {
    throw new Error('contract must be an object')
  }
  assertAllowedKeys(contract, [
    'id', 'goal', 'acceptance_criteria', 'boundary_constraints',
    'dependencies', 'relevant_state_ids', 'relevant_audit_ids',
    'allowed_tools', 'max_tool_calls', 'timeout_seconds',
  ], 'contract')
  const id = asId(contract.id, 'contract.id')
  const goal = asNonEmptyString(contract.goal, 'contract.goal')
  const acceptance = asStringArray(contract.acceptance_criteria ?? [], 'contract.acceptance_criteria')
  if (acceptance.length === 0) throw new Error('contract.acceptance_criteria must be non-empty')
  const boundary = asStringArray(contract.boundary_constraints ?? [], 'contract.boundary_constraints', { allowEmpty: true })
  const dependencies = asStringArray(contract.dependencies ?? [], 'contract.dependencies', { allowEmpty: true })
  const relevantState = asStringArray(contract.relevant_state_ids ?? [], 'contract.relevant_state_ids', { allowEmpty: true })
  const relevantAudit = asStringArray(contract.relevant_audit_ids ?? [], 'contract.relevant_audit_ids', { allowEmpty: true })

  const allowed = contract.allowed_tools ?? []
  if (!Array.isArray(allowed) || allowed.length === 0) {
    throw new Error('contract.allowed_tools must be a non-empty array')
  }
  for (const tool of allowed) {
    if (!SHOP_TOOL_NAMES.has(tool)) {
      throw new Error(`contract.allowed_tools contains unsupported tool "${tool}"`)
    }
  }
  const maxCalls = contract.max_tool_calls
  if (!Number.isInteger(maxCalls) || maxCalls < 1 || maxCalls > 100) {
    throw new Error('contract.max_tool_calls must be an integer in 1..100')
  }
  const timeout = contract.timeout_seconds
  if (!Number.isFinite(timeout) || timeout <= 0 || timeout > 86400) {
    throw new Error('contract.timeout_seconds must be a positive finite number (<= 86400)')
  }
  return {
    id,
    goal,
    acceptance_criteria: acceptance,
    boundary_constraints: boundary,
    dependencies,
    relevant_state_ids: relevantState,
    relevant_audit_ids: relevantAudit,
    allowed_tools: [...allowed],
    max_tool_calls: maxCalls,
    timeout_seconds: timeout,
  }
}

function validateEvidenceBlock(evidence) {
  if (!evidence || typeof evidence !== 'object') {
    throw new Error('audit.evidence items must be objects')
  }
  assertAllowedKeys(evidence, ['id', 'kind', 'summary'], 'audit.evidence item')
  const id = asId(evidence.id, 'audit.evidence[].id')
  const kind = evidence.kind
  if (kind !== 'read_only_observation' && kind !== 'read_only_environment_state') {
    throw new Error(`audit.evidence[].kind must be read-only, got "${kind}"`)
  }
  const summary = asNonEmptyString(evidence.summary, 'audit.evidence[].summary')
  return { id, kind, summary }
}

/**
 * 校验 Audit Report 最小 schema。
 * status_updates 里对 completed 的引用会在 reducer 中进一步核验归属与完整性；
 * 这里只保证结构合法。
 */
export function validateAuditReport(audit) {
  if (!audit || typeof audit !== 'object' || Array.isArray(audit)) {
    throw new Error('audit report must be an object')
  }
  assertAllowedKeys(audit, [
    'id', 'round', 'contract_id', 'status', 'integrity',
    'verified_summary', 'evidence', 'evidence_ids', 'new_facts',
    'status_updates', 'open_gaps',
  ], 'audit report')
  const id = asId(audit.id, 'audit.id')
  const status = audit.status
  if (!AUDIT_STATUS.has(status)) throw new Error(`invalid audit status "${status}"`)
  const integrity = audit.integrity
  if (!AUDIT_INTEGRITY.has(integrity)) throw new Error(`invalid audit integrity "${integrity}"`)
  const summary = asNonEmptyString(audit.verified_summary, 'audit.verified_summary')

  const evidence = []
  for (const e of audit.evidence ?? []) {
    evidence.push(validateEvidenceBlock(e))
  }
  const evidenceIds = asStringArray(audit.evidence_ids ?? [], 'audit.evidence_ids', { max: 200 })
  // 两种写法只能出现其一；若同时存在，必须完全一致。
  if (evidence.length > 0 && evidenceIds.length > 0) {
    const a = evidence.map(e => e.id).sort()
    const b = [...evidenceIds].sort()
    if (JSON.stringify(a) !== JSON.stringify(b)) {
      throw new Error('audit.evidence and audit.evidence_ids disagree')
    }
  }

  const openGaps = asStringArray(audit.open_gaps ?? [], 'audit.open_gaps', { allowEmpty: true })

  const newFacts = []
  for (const f of audit.new_facts ?? []) {
    if (!f || typeof f !== 'object') throw new Error('audit.new_facts items must be objects')
    assertAllowedKeys(f, ['id', 'text', 'status', 'evidence_refs'], 'audit.new_facts item')
    const factId = f.id ?? null
    const factText = asNonEmptyString(f.text, 'audit.new_facts[].text')
    const factStatus = f.status ?? 'pending'
    if (!RECORD_STATUS.has(factStatus)) throw new Error(`invalid new_facts[].status "${factStatus}"`)
    const factRefs = asStringArray(f.evidence_refs ?? [], 'audit.new_facts[].evidence_refs', { max: 50 })
    newFacts.push({ id: factId, text: factText, status: factStatus, evidence_refs: factRefs })
  }

  const statusUpdates = []
  for (const u of audit.status_updates ?? []) {
    if (!u || typeof u !== 'object') throw new Error('audit.status_updates items must be objects')
    assertAllowedKeys(u, ['id', 'status', 'evidence_refs'], 'audit.status_updates item')
    const updateId = asId(u.id, 'audit.status_updates[].id')
    const updateStatus = u.status
    if (!RECORD_STATUS.has(updateStatus)) throw new Error(`invalid status_updates[].status "${updateStatus}"`)
    const refs = asStringArray(u.evidence_refs ?? [], 'audit.status_updates[].evidence_refs', { max: 50 })
    if (updateStatus === 'completed' && refs.length === 0) {
      throw new Error('audit.status_updates[].completed requires evidence_refs')
    }
    statusUpdates.push({ id: updateId, status: updateStatus, evidence_refs: refs })
  }

  return {
    id,
    round: Number.isInteger(audit.round) ? audit.round : null,
    contract_id: audit.contract_id ?? null,
    status,
    integrity,
    verified_summary: summary,
    evidence,
    evidence_ids: evidenceIds,
    new_facts: newFacts,
    status_updates: statusUpdates,
    open_gaps: openGaps,
  }
}

/** 便捷：从 Audit Report 的 evidence 派生 evidence_ids（供 reducer 使用）。 */
export function evidenceIdsFromReport(audit) {
  return (audit.evidence ?? []).map(e => e.id)
}

/** 便捷：构造一条只读证据块。 */
export function makeEvidence({ id, kind = 'read_only_observation', summary }) {
  return { id, kind, summary }
}
