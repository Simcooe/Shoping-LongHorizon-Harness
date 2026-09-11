/** mea-v4 structured Task State reducer (stage 2). */

import {
  TASK_STATE_SCHEMA,
  evidenceCoversScope,
  resolveEvidenceRef,
  sameScope,
  validateAuditReport,
  validateManagerOutput,
  validateScope,
  validateTaskState,
} from './schema.js'

const RECORD_STATUS = new Set(['pending', 'completed', 'blocked', 'untrusted'])

function copy(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value))
}

function asNonEmptyString(value, field) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`${field} must be a non-empty string`)
  }
  return value
}

function listNameForId(id) {
  if (id.startsWith('req-')) return 'requirements'
  if (id.startsWith('art-')) return 'artifacts'
  if (id.startsWith('fact-')) return 'facts'
  throw new Error(`unknown record id "${id}"`)
}

function findRecord(state, id) {
  return state[listNameForId(id)].find(record => record.id === id) ?? null
}

function setRecord(state, record) {
  const list = state[listNameForId(record.id)]
  const index = list.findIndex(item => item.id === record.id)
  if (index === -1) list.push(record)
  else list[index] = record
}

function makeRecord({
  id, type, content, source, status = 'pending', lifecycle = 'active',
  requirementVersion = 1, scope = null, dependencies = [], evidenceRefs = [],
  valid = true, invalidReason = null, history = [],
}) {
  if (!RECORD_STATUS.has(status)) throw new Error(`invalid record status "${status}"`)
  return {
    id: asNonEmptyString(id, 'record.id'),
    type: asNonEmptyString(type, 'record.type'),
    content: asNonEmptyString(content, 'record.content'),
    source: asNonEmptyString(source, 'record.source'),
    status,
    lifecycle,
    requirement_version: requirementVersion,
    scope: validateScope(scope),
    dependencies: [...dependencies],
    evidence_refs: copy(evidenceRefs),
    history: copy(history),
    valid,
    invalid_reason: invalidReason,
  }
}

function allocateRecordId(state, prefix) {
  const used = new Set([
    ...state.requirements, ...state.artifacts, ...state.facts,
  ].map(record => record.id))
  let number = 1
  while (used.has(`${prefix}-${number}`)) number += 1
  return `${prefix}-${number}`
}

function scopeMatchesSelector(scope, selector) {
  const actual = validateScope(scope)
  const expected = validateScope(selector)
  if (actual === null || expected === null) return false
  return Object.entries(expected).every(([key, value]) => actual[key] === value)
}

function allRecords(state) {
  return [...state.requirements, ...state.artifacts, ...state.facts]
}

function invalidateRecord(record, reason) {
  record.history.push({
    version: record.requirement_version,
    from_status: record.status,
    to_status: 'untrusted',
    lifecycle: record.lifecycle,
    valid: record.valid,
    reason,
  })
  record.valid = record.type === 'requirement'
  record.status = 'untrusted'
  record.evidence_refs = []
  record.invalid_reason = reason
}

function invalidateIdsAndDependents(state, rootIds, reason, { includeRoots = true } = {}) {
  const records = allRecords(state)
  const affected = new Set(rootIds)
  let changed = true
  while (changed) {
    changed = false
    for (const record of records) {
      if (affected.has(record.id)) continue
      if (record.dependencies.some(dependency => affected.has(dependency))) {
        affected.add(record.id)
        changed = true
      }
    }
  }
  for (const record of records) {
    if (!affected.has(record.id)) continue
    if (!includeRoots && rootIds.has(record.id)) continue
    if (record.valid || record.status !== 'untrusted') invalidateRecord(record, reason)
  }
}

export function createTaskState({
  taskId,
  originalGoal,
  personaText = null,
  initialRequirements = [],
  round = 0,
} = {}) {
  if (!Array.isArray(initialRequirements) || initialRequirements.length > 100) {
    throw new Error('initialRequirements must be an array (max 100)')
  }
  const state = {
    schema: TASK_STATE_SCHEMA,
    task: {
      id: asNonEmptyString(taskId, 'taskId'),
      original_goal: asNonEmptyString(originalGoal, 'originalGoal'),
      persona: personaText,
    },
    requirements: initialRequirements.map((requirement, index) => makeRecord({
      id: `req-${index + 1}`,
      type: 'requirement',
      content: requirement.text,
      source: requirement.source === 'shopper_reply' ? 'shopper_reply' : 'initial_request',
    })),
    artifacts: [],
    facts: [],
    shopper_replies: [],
    audit_history: [],
    integrity_issues: [],
    round: Number.isInteger(round) && round >= 0 ? round : 0,
    decision: { kind: 'planning', reason: 'initial state' },
    contract: null,
    question: null,
  }
  validateTaskState(state)
  return state
}

function validateAuditRefsAgainstState(state, audit) {
  const prospective = copy(state)
  prospective.audit_history.push(audit)
  for (const finding of audit.findings) {
    for (const ref of finding.evidence_refs) resolveEvidenceRef(prospective, ref)
  }
}

/** Append an audit and integrity issues. It does not mutate task records. */
export function applyAuditReport(state, auditReport, { resolveObservation = null } = {}) {
  validateTaskState(state)
  const audit = validateAuditReport(auditReport)
  if (state.audit_history.some(item => item.id === audit.id)) {
    throw new Error(`duplicate audit id "${audit.id}"`)
  }
  if (state.decision.kind !== 'execute' || !state.contract) {
    throw new Error('audit requires an active execute contract')
  }
  if (audit.contract_id !== state.contract.id) {
    throw new Error(`audit contract "${audit.contract_id}" does not match active contract`)
  }
  if (audit.round !== state.round + 1) {
    throw new Error(`audit round must be ${state.round + 1}`)
  }
  validateAuditRefsAgainstState(state, audit)
  if (resolveObservation !== null) {
    if (typeof resolveObservation !== 'function') throw new Error('resolveObservation must be a function')
    for (const evidence of audit.evidence) resolveObservation(evidence.observation_ref)
  }
  const result = copy(state)
  if (audit.resolves_issue_ids.length > 0 && audit.integrity !== 'clean') {
    throw new Error('only a clean audit may resolve integrity issues')
  }
  for (const issueId of audit.resolves_issue_ids) {
    const issue = result.integrity_issues.find(item => item.id === issueId)
    if (!issue || issue.status !== 'open') throw new Error(`cannot resolve integrity issue "${issueId}"`)
    issue.status = 'resolved'
    issue.resolved_by_audit = audit.id
  }
  result.audit_history.push(audit)
  if (audit.integrity === 'violation') {
    result.integrity_issues.push({
      id: `issue-${audit.id}`,
      status: 'open',
      opened_by_audit: audit.id,
      resolved_by_audit: null,
      summary: audit.verified_summary,
    })
  }
  result.round = Math.max(result.round, audit.round)
  result.decision = { kind: 'planning', reason: `audit ${audit.id} ready for manager` }
  result.contract = null
  result.question = null
  validateTaskState(result)
  return result
}

function resolveFinding(state, update) {
  const audit = state.audit_history.find(item => item.id === update.audit_id)
  if (!audit) throw new Error(`unknown audit "${update.audit_id}"`)
  const finding = audit.findings.find(item => item.finding_id === update.finding_id)
  if (!finding) throw new Error(`unknown finding "${update.audit_id}/${update.finding_id}"`)
  if (finding.record_id !== update.record_id) {
    throw new Error('manager update record_id differs from audited finding')
  }
  for (const ref of finding.evidence_refs) resolveEvidenceRef(state, ref)
  return { audit, finding }
}

function applyAuditedFinding(state, update) {
  const { audit, finding } = resolveFinding(state, update)
  if (finding.proposed_status === 'completed' && audit.integrity !== 'clean') {
    throw new Error('completed record requires clean audit')
  }
  let record = findRecord(state, finding.record_id)
  if (record?.type === 'requirement') {
    if (record.lifecycle !== 'active') throw new Error('cannot update a revoked requirement')
    if (finding.requirement_version !== record.requirement_version) {
      throw new Error(`finding targets stale requirement version ${finding.requirement_version}; current is ${record.requirement_version}`)
    }
  }
  if (record?.history.some(item => item.audit_id === audit.id
    && item.finding_id === finding.finding_id)) {
    return
  }
  if (!record) {
    if (finding.record_id.startsWith('req-')) {
      throw new Error('new requirements must come from a real shopper reply')
    }
    if (!finding.supported || !finding.content) {
      throw new Error('new fact/artifact requires a supported finding with content')
    }
    record = makeRecord({
      id: finding.record_id,
      type: finding.record_id.startsWith('art-') ? 'artifact' : 'fact',
      content: finding.content,
      source: `audit:${audit.id}/${finding.finding_id}`,
      scope: finding.scope,
      dependencies: finding.dependencies,
    })
    setRecord(state, record)
  } else if (record.type === 'requirement') {
    if (finding.content !== null && finding.content !== record.content) {
      throw new Error('audit/manager cannot rewrite requirement content')
    }
    if (finding.scope !== null && !sameScope(finding.scope, record.scope)) {
      throw new Error('audit/manager cannot rewrite requirement scope')
    }
  } else {
    if (finding.scope !== null && record.scope !== null
      && !sameScope(finding.scope, record.scope)) {
      throw new Error('use a new record id when fact/artifact scope changes')
    }
    if (finding.dependencies.length > 0) {
      if (record.dependencies.length > 0
        && JSON.stringify(record.dependencies) !== JSON.stringify(finding.dependencies)) {
        throw new Error('use a new record id when dependencies change')
      }
      record.dependencies = [...finding.dependencies]
    }
    if (finding.content !== null) record.content = finding.content
    if (finding.scope !== null) record.scope = finding.scope
  }
  if (record.valid === false && finding.proposed_status === 'completed') {
    throw new Error('cannot complete an invalidated record')
  }
  for (const ref of finding.evidence_refs) {
    const { evidence } = resolveEvidenceRef(state, ref)
    if (!evidenceCoversScope(record.scope, evidence.scope)) {
      throw new Error(`evidence scope does not cover record "${record.id}"`)
    }
  }
  record.history.push({
    version: record.requirement_version,
    from_status: record.status,
    to_status: finding.supported ? finding.proposed_status : 'pending',
    audit_id: audit.id,
    finding_id: finding.finding_id,
    evidence_refs: copy(finding.evidence_refs),
    summary: finding.summary,
  })
  record.status = finding.supported ? finding.proposed_status : 'pending'
  record.evidence_refs = copy(finding.evidence_refs)
  record.valid = true
  record.invalid_reason = null
}

export function applyManagerOutput(state, managerOutput) {
  validateTaskState(state)
  const output = validateManagerOutput(managerOutput)
  const result = copy(state)
  for (const update of output.state_updates) applyAuditedFinding(result, update)
  if (output.contract && result.audit_history.some(
    audit => audit.contract_id === output.contract.id)) {
    throw new Error(`contract id "${output.contract.id}" has already been used`)
  }
  result.decision = { kind: output.decision, reason: output.reason }
  result.contract = output.contract
  result.question = output.question
  if (output.decision === 'done') {
    const verdict = finalize(result)
    if (!verdict.ok) throw new Error(`manager done rejected: ${verdict.reason}`)
  }
  validateTaskState(result)
  return result
}

export function recordShopperReply(state, {
  id, question, reply, eventRef, recordedAt = Date.now(),
} = {}) {
  validateTaskState(state)
  const result = copy(state)
  const replyId = asNonEmptyString(id, 'shopper_reply.id')
  if (result.shopper_replies.some(item => item.id === replyId)) {
    throw new Error(`duplicate shopper reply id "${replyId}"`)
  }
  result.shopper_replies.push({
    id: replyId,
    question: asNonEmptyString(question, 'shopper_reply.question'),
    reply: asNonEmptyString(reply, 'shopper_reply.reply'),
    event_ref: asNonEmptyString(eventRef, 'shopper_reply.eventRef'),
    recorded_at: recordedAt,
  })
  validateTaskState(result)
  return result
}

export function addRequirementFromReply(state, { text, scope = null } = {}) {
  validateTaskState(state)
  const result = copy(state)
  const id = allocateRecordId(result, 'req')
  result.requirements.push(makeRecord({
    id,
    type: 'requirement',
    content: asNonEmptyString(text, 'requirement.text'),
    source: 'shopper_reply',
    scope,
  }))
  result.decision = { kind: 'planning', reason: 'new shopper requirement' }
  result.contract = null
  result.question = null
  validateTaskState(result)
  return result
}

export function modifyRequirementFromReply(state, { id, text, scope } = {}) {
  validateTaskState(state)
  const result = copy(state)
  const record = findRecord(result, id)
  if (!record || record.type !== 'requirement') throw new Error(`requirement "${id}" not found`)
  record.history.push({
    version: record.requirement_version,
    content: record.content,
    status: record.status,
    scope: copy(record.scope),
    evidence_refs: copy(record.evidence_refs),
    lifecycle: record.lifecycle,
    valid: record.valid,
    reason: 'modified by shopper reply',
  })
  record.content = asNonEmptyString(text, 'requirement.text')
  if (scope !== undefined) record.scope = validateScope(scope)
  record.requirement_version += 1
  record.status = 'pending'
  record.evidence_refs = []
  record.lifecycle = 'active'
  record.valid = true
  record.invalid_reason = null
  invalidateIdsAndDependents(result, new Set([id]), 'dependency requirement modified', {
    includeRoots: false,
  })
  result.decision = { kind: 'planning', reason: 'shopper requirement modified' }
  result.contract = null
  result.question = null
  validateTaskState(result)
  return result
}

export function revokeRequirementFromReply(state, { id, reason = 'revoked by shopper reply' } = {}) {
  validateTaskState(state)
  const result = copy(state)
  const record = findRecord(result, id)
  if (!record || record.type !== 'requirement') throw new Error(`requirement "${id}" not found`)
  if (record.lifecycle === 'revoked') throw new Error(`requirement "${id}" is already revoked`)
  invalidateIdsAndDependents(result, new Set([id]), asNonEmptyString(reason, 'revoke.reason'), {
    includeRoots: false,
  })
  record.history.push({
    version: record.requirement_version,
    content: record.content,
    status: record.status,
    lifecycle: record.lifecycle,
    scope: copy(record.scope),
    evidence_refs: copy(record.evidence_refs),
    valid: record.valid,
    reason,
  })
  record.lifecycle = 'revoked'
  record.status = 'untrusted'
  record.valid = false
  record.evidence_refs = []
  record.invalid_reason = reason
  record.requirement_version += 1
  result.decision = { kind: 'planning', reason: 'shopper requirement revoked' }
  result.contract = null
  result.question = null
  validateTaskState(result)
  return result
}

export function addShopperReply(state, { text, scope = null, modifyId = null } = {}) {
  return modifyId
    ? modifyRequirementFromReply(state, { id: modifyId, text, scope })
    : addRequirementFromReply(state, { text, scope })
}

export function invalidateRequirements(state, {
  scope = null,
  recordIds = [],
  reason = 'scope changed',
} = {}) {
  validateTaskState(state)
  const result = copy(state)
  const roots = new Set(recordIds)
  if (scope !== null) {
    for (const record of allRecords(result)) {
      if (scopeMatchesSelector(record.scope, scope)) roots.add(record.id)
    }
  }
  invalidateIdsAndDependents(result, roots, reason)
  validateTaskState(result)
  return result
}

export function finalize(state, reason = 'all requirements completed') {
  validateTaskState(state)
  const openIssues = state.integrity_issues.filter(issue => issue.status === 'open')
  if (openIssues.length > 0) {
    return { ok: false, reason: `open integrity issues: ${openIssues.map(i => i.id).join(', ')}` }
  }
  const finalAudit = state.audit_history[state.audit_history.length - 1]
  if (!finalAudit || finalAudit.status !== 'complete' || finalAudit.integrity !== 'clean') {
    return { ok: false, reason: 'final audit must be complete and clean' }
  }
  const active = state.requirements.filter(record => record.lifecycle === 'active')
  const blocked = active.filter(record => record.status === 'blocked')
  if (blocked.length > 0) {
    return { ok: false, reason: `blocked requirements: ${blocked.map(r => r.id).join(', ')}` }
  }
  const incomplete = active.filter(record => record.status !== 'completed')
  if (incomplete.length > 0) {
    return { ok: false, reason: `incomplete requirements: ${incomplete.map(r => r.id).join(', ')}` }
  }
  return { ok: true, reason }
}

export function applyDecision(state, payload = {}) {
  validateTaskState(state)
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('decision must be an object')
  }
  const allowed = new Set(['kind', 'reason'])
  for (const key of Object.keys(payload)) {
    if (!allowed.has(key)) throw new Error(`unknown decision field "${key}"`)
  }
  if (payload.kind === 'done') {
    const verdict = finalize(state)
    if (!verdict.ok) throw new Error(`manager done rejected: ${verdict.reason}`)
  } else if (payload.kind !== 'blocked') {
    throw new Error('execute/ask decisions require applyManagerOutput')
  }
  const result = copy(state)
  result.decision = {
    kind: payload.kind,
    reason: asNonEmptyString(payload.reason, 'decision.reason'),
  }
  result.contract = null
  result.question = null
  validateTaskState(result)
  return result
}

export function cloneState(state) {
  validateTaskState(state)
  return copy(state)
}
