/**
 * mea-v4 paper-alignment schemas (stage 2).
 * Auditors create evidence/findings; managers may only select validated findings.
 */

export const CONTRACT_SCHEMA = 'longhorizon-contract-v3'
export const AUDIT_SCHEMA = 'longhorizon-audit-v3'
export const TASK_STATE_SCHEMA = 'longhorizon-task-state-v3'

export const SHOP_TOOL_NAMES = ['search', 'click', 'finish', 'ask_shopper']
export const RECORD_STATUSES = ['pending', 'completed', 'blocked', 'untrusted']
export const MANAGER_DECISIONS = ['execute', 'done', 'blocked', 'ask']

const SHOP_TOOL_SET = new Set(SHOP_TOOL_NAMES)
const RECORD_STATUS_SET = new Set(RECORD_STATUSES)
const MANAGER_DECISION_SET = new Set(MANAGER_DECISIONS)
const STATE_DECISION_SET = new Set(['planning', ...MANAGER_DECISIONS])
const AUDIT_STATUS_SET = new Set(['complete', 'incomplete', 'blocked'])
const AUDIT_INTEGRITY_SET = new Set(['clean', 'suspect', 'violation'])
const EVIDENCE_KIND_SET = new Set([
  'read_only_observation',
  'read_only_environment_state',
  'read_only_terminal_receipt',
])
const RECORD_TYPES = new Set(['requirement', 'artifact', 'fact'])
const RECORD_LIFECYCLES = new Set(['active', 'revoked'])
const RECORD_PREFIX = { requirement: 'req-', artifact: 'art-', fact: 'fact-' }

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function copy(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value))
}

function assertAllowedKeys(obj, allowed, context) {
  for (const key of Object.keys(obj)) {
    if (!allowed.includes(key)) throw new Error(`unknown ${context} field "${key}"`)
  }
}

function asNonEmptyString(value, field, max = 4000) {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) {
    throw new Error(`${field} must be a non-empty bounded string (max ${max})`)
  }
  return value
}

function asStringArray(value, field, { max = 100, allowEmpty = true, maxLen = 2000 } = {}) {
  if (!Array.isArray(value) || value.length > max) {
    throw new Error(`${field} must be an array (max ${max})`)
  }
  const out = value.map(item => asNonEmptyString(item, `${field}[]`, maxLen))
  if (!allowEmpty && out.length === 0) throw new Error(`${field} must be non-empty`)
  if (new Set(out).size !== out.length) throw new Error(`${field} must not contain duplicates`)
  return out
}

export function validateScope(value, field = 'scope') {
  if (value === undefined || value === null) return null
  if (!isObject(value)) throw new Error(`${field} must be an object or null`)
  const out = {}
  for (const key of Object.keys(value).sort()) {
    const item = value[key]
    if (item === undefined || item === null) continue
    if (!['string', 'number', 'boolean'].includes(typeof item)) {
      throw new Error(`${field}.${key} must be a scalar`)
    }
    out[key] = item
  }
  return out
}

export function sameScope(left, right) {
  return JSON.stringify(validateScope(left)) === JSON.stringify(validateScope(right))
}

export function evidenceCoversScope(recordScope, evidenceScope) {
  const required = validateScope(recordScope)
  if (required === null) return true
  const actual = validateScope(evidenceScope)
  if (actual === null) return false
  return Object.entries(required).every(([key, value]) => actual[key] === value)
}

export function isRecordId(id) {
  return typeof id === 'string'
    && Object.values(RECORD_PREFIX).some(prefix => id.startsWith(prefix))
}

export function validateEvidenceRef(value, field = 'evidence_ref') {
  if (!isObject(value)) throw new Error(`${field} must be an object`)
  assertAllowedKeys(value, ['audit_id', 'evidence_id'], field)
  return {
    audit_id: asNonEmptyString(value.audit_id, `${field}.audit_id`, 200),
    evidence_id: asNonEmptyString(value.evidence_id, `${field}.evidence_id`, 200),
  }
}

function validateEvidenceRefs(value, field, { allowEmpty = true } = {}) {
  if (!Array.isArray(value) || value.length > 100) {
    throw new Error(`${field} must be an array (max 100)`)
  }
  const refs = value.map((ref, index) => validateEvidenceRef(ref, `${field}[${index}]`))
  if (!allowEmpty && refs.length === 0) throw new Error(`${field} must be non-empty`)
  const keys = refs.map(ref => `${ref.audit_id}/${ref.evidence_id}`)
  if (new Set(keys).size !== keys.length) throw new Error(`${field} contains duplicate refs`)
  return refs
}

export function validateContract(contract) {
  if (!isObject(contract)) throw new Error('contract must be an object')
  assertAllowedKeys(contract, [
    'schema', 'id', 'goal', 'acceptance_criteria', 'boundary_constraints',
    'dependencies', 'relevant_state_ids', 'relevant_audit_ids',
    'role_tools', 'manager_suggested_tools', 'tool_rules', 'budget',
  ], 'contract')
  if (contract.schema !== undefined && contract.schema !== CONTRACT_SCHEMA) {
    throw new Error(`unsupported contract schema "${contract.schema}"`)
  }
  const id = asNonEmptyString(contract.id, 'contract.id', 200)
  const goal = asNonEmptyString(contract.goal, 'contract.goal')
  const acceptanceCriteria = asStringArray(
    contract.acceptance_criteria ?? [], 'contract.acceptance_criteria', { allowEmpty: false })
  const boundaryConstraints = asStringArray(
    contract.boundary_constraints ?? [], 'contract.boundary_constraints')
  const dependencies = asStringArray(contract.dependencies ?? [], 'contract.dependencies')
  const relevantStateIds = asStringArray(
    contract.relevant_state_ids ?? [], 'contract.relevant_state_ids')
  const relevantAuditIds = asStringArray(
    contract.relevant_audit_ids ?? [], 'contract.relevant_audit_ids')
  for (const recordId of [...dependencies, ...relevantStateIds]) {
    if (!isRecordId(recordId)) throw new Error(`contract references invalid record id "${recordId}"`)
  }

  const roleTools = asStringArray(contract.role_tools ?? [], 'contract.role_tools', {
    max: SHOP_TOOL_NAMES.length, allowEmpty: false, maxLen: 100,
  })
  for (const tool of roleTools) {
    if (!SHOP_TOOL_SET.has(tool)) throw new Error(`contract.role_tools contains unsupported tool "${tool}"`)
  }
  const suggestedTools = asStringArray(
    contract.manager_suggested_tools ?? [], 'contract.manager_suggested_tools', {
      max: SHOP_TOOL_NAMES.length, maxLen: 100,
    })
  for (const tool of suggestedTools) {
    if (!roleTools.includes(tool)) {
      throw new Error(`contract.manager_suggested_tools "${tool}" is not in role_tools`)
    }
  }

  const toolRules = contract.tool_rules ?? []
  if (!Array.isArray(toolRules) || toolRules.length > 50) {
    throw new Error('contract.tool_rules must be an array (max 50)')
  }
  const normalizedRules = toolRules.map((rule, index) => {
    if (!isObject(rule)) throw new Error(`contract.tool_rules[${index}] must be an object`)
    assertAllowedKeys(rule, ['tool', 'allow', 'allow_kinds', 'deny_kinds', 'deny_values'], 'tool rule')
    const tool = asNonEmptyString(rule.tool, 'tool_rule.tool', 100)
    if (!roleTools.includes(tool)) throw new Error(`tool rule references unavailable tool "${tool}"`)
    if (rule.allow !== undefined && typeof rule.allow !== 'boolean') {
      throw new Error('tool_rule.allow must be a boolean')
    }
    const allowKinds = asStringArray(rule.allow_kinds ?? [], 'tool_rule.allow_kinds')
    const denyKinds = asStringArray(rule.deny_kinds ?? [], 'tool_rule.deny_kinds')
    const denyValues = asStringArray(rule.deny_values ?? [], 'tool_rule.deny_values')
    if (rule.allow === undefined && allowKinds.length === 0
      && denyKinds.length === 0 && denyValues.length === 0) {
      throw new Error(`tool rule for "${tool}" has no constraint`)
    }
    return {
      tool,
      ...(rule.allow === undefined ? {} : { allow: rule.allow }),
      allow_kinds: allowKinds,
      deny_kinds: denyKinds,
      deny_values: denyValues,
    }
  })

  const budget = contract.budget
  if (!isObject(budget)) throw new Error('contract.budget must be an object')
  assertAllowedKeys(budget, ['max_tool_calls', 'timeout_seconds', 'max_tokens'], 'contract.budget')
  if (!Number.isInteger(budget.max_tool_calls)
    || budget.max_tool_calls < 1 || budget.max_tool_calls > 100) {
    throw new Error('contract.budget.max_tool_calls must be an integer in 1..100')
  }
  if (!Number.isFinite(budget.timeout_seconds)
    || budget.timeout_seconds <= 0 || budget.timeout_seconds > 86400) {
    throw new Error('contract.budget.timeout_seconds must be in (0, 86400]')
  }
  if (budget.max_tokens !== undefined
    && (!Number.isInteger(budget.max_tokens) || budget.max_tokens < 1)) {
    throw new Error('contract.budget.max_tokens must be a positive integer')
  }

  return {
    schema: CONTRACT_SCHEMA,
    id,
    goal,
    acceptance_criteria: acceptanceCriteria,
    boundary_constraints: boundaryConstraints,
    dependencies,
    relevant_state_ids: relevantStateIds,
    relevant_audit_ids: relevantAuditIds,
    role_tools: roleTools,
    manager_suggested_tools: suggestedTools,
    tool_rules: normalizedRules,
    budget: copy(budget),
  }
}

export function validateObservationRef(value, field = 'observation_ref') {
  if (!isObject(value)) throw new Error(`${field} must be an object`)
  assertAllowedKeys(value, ['event_id', 'snapshot_id', 'environment_session'], field)
  return {
    event_id: asNonEmptyString(value.event_id, `${field}.event_id`, 200),
    snapshot_id: asNonEmptyString(value.snapshot_id, `${field}.snapshot_id`, 200),
    environment_session: asNonEmptyString(
      value.environment_session, `${field}.environment_session`, 200),
  }
}

export function validateEvidenceBlock(evidence) {
  if (!isObject(evidence)) throw new Error('audit.evidence items must be objects')
  assertAllowedKeys(evidence, ['id', 'kind', 'summary', 'scope', 'observation_ref'], 'audit evidence')
  const id = asNonEmptyString(evidence.id, 'audit.evidence[].id', 200)
  if (!EVIDENCE_KIND_SET.has(evidence.kind)) {
    throw new Error(`invalid read-only evidence kind "${evidence.kind}"`)
  }
  return {
    id,
    kind: evidence.kind,
    summary: asNonEmptyString(evidence.summary, 'audit.evidence[].summary'),
    scope: validateScope(evidence.scope, 'audit.evidence[].scope'),
    observation_ref: validateObservationRef(
      evidence.observation_ref, 'audit.evidence[].observation_ref'),
  }
}

function validateFinding(finding) {
  if (!isObject(finding)) throw new Error('audit.findings items must be objects')
  assertAllowedKeys(finding, [
    'finding_id', 'record_id', 'requirement_version', 'criterion', 'supported',
    'proposed_status', 'evidence_refs', 'dependencies', 'summary', 'content', 'scope',
  ], 'audit finding')
  const findingId = asNonEmptyString(finding.finding_id, 'finding.finding_id', 200)
  const recordId = finding.record_id ?? null
  if (recordId !== null && !isRecordId(recordId)) {
    throw new Error(`finding.record_id "${recordId}" is invalid`)
  }
  if (typeof finding.supported !== 'boolean') throw new Error('finding.supported must be boolean')
  if (!RECORD_STATUS_SET.has(finding.proposed_status)) {
    throw new Error(`invalid finding.proposed_status "${finding.proposed_status}"`)
  }
  if (!finding.supported && finding.proposed_status === 'completed') {
    throw new Error('unsupported finding cannot propose completed')
  }
  let requirementVersion = finding.requirement_version ?? null
  if (recordId?.startsWith('req-')) {
    if (!Number.isInteger(requirementVersion) || requirementVersion < 1) {
      throw new Error('requirement finding requires a positive requirement_version')
    }
  } else if (requirementVersion !== null) {
    throw new Error('requirement_version is only valid for requirement findings')
  }
  const refs = validateEvidenceRefs(
    finding.evidence_refs ?? [], 'finding.evidence_refs', {
      allowEmpty: finding.proposed_status !== 'completed',
    })
  return {
    finding_id: findingId,
    record_id: recordId,
    requirement_version: requirementVersion,
    criterion: asNonEmptyString(finding.criterion, 'finding.criterion'),
    supported: finding.supported,
    proposed_status: finding.proposed_status,
    evidence_refs: refs,
    dependencies: asStringArray(finding.dependencies ?? [], 'finding.dependencies'),
    summary: asNonEmptyString(finding.summary, 'finding.summary'),
    content: finding.content === undefined || finding.content === null
      ? null : asNonEmptyString(finding.content, 'finding.content'),
    scope: validateScope(finding.scope, 'finding.scope'),
  }
}

export function validateAuditReport(audit) {
  if (!isObject(audit)) throw new Error('audit report must be an object')
  assertAllowedKeys(audit, [
    'schema', 'id', 'round', 'contract_id', 'status', 'integrity',
    'verified_summary', 'evidence', 'findings', 'remaining_gaps',
    'suggested_updates', 'resolves_issue_ids',
  ], 'audit report')
  if (audit.schema !== undefined && audit.schema !== AUDIT_SCHEMA) {
    throw new Error(`unsupported audit schema "${audit.schema}"`)
  }
  const id = asNonEmptyString(audit.id, 'audit.id', 200)
  if (!Number.isInteger(audit.round) || audit.round < 1) {
    throw new Error('audit.round must be a positive integer')
  }
  const contractId = asNonEmptyString(audit.contract_id, 'audit.contract_id', 200)
  if (!AUDIT_STATUS_SET.has(audit.status)) throw new Error(`invalid audit status "${audit.status}"`)
  if (!AUDIT_INTEGRITY_SET.has(audit.integrity)) {
    throw new Error(`invalid audit integrity "${audit.integrity}"`)
  }
  const evidence = (audit.evidence ?? []).map(validateEvidenceBlock)
  const evidenceIds = evidence.map(item => item.id)
  if (new Set(evidenceIds).size !== evidenceIds.length) throw new Error('duplicate evidence id in audit')
  const findings = (audit.findings ?? []).map(validateFinding)
  const findingIds = findings.map(item => item.finding_id)
  if (new Set(findingIds).size !== findingIds.length) throw new Error('duplicate finding id in audit')
  const findingById = new Map(findings.map(item => [item.finding_id, item]))
  for (const finding of findings) {
    for (const ref of finding.evidence_refs) {
      if (ref.audit_id === id && !evidenceIds.includes(ref.evidence_id)) {
        throw new Error(`finding references missing evidence "${id}/${ref.evidence_id}"`)
      }
    }
  }

  const suggestedUpdates = (audit.suggested_updates ?? []).map((update, index) => {
    if (!isObject(update)) throw new Error(`suggested_updates[${index}] must be an object`)
    assertAllowedKeys(update, ['finding_id', 'record_id'], 'suggested update')
    const findingId = asNonEmptyString(update.finding_id, 'suggested_update.finding_id', 200)
    const recordId = asNonEmptyString(update.record_id, 'suggested_update.record_id', 200)
    const finding = findingById.get(findingId)
    if (!finding) throw new Error(`suggested update references missing finding "${findingId}"`)
    if (finding.record_id !== recordId) throw new Error('suggested update record_id differs from finding')
    return { finding_id: findingId, record_id: recordId }
  })
  return {
    schema: AUDIT_SCHEMA,
    id,
    round: audit.round,
    contract_id: contractId,
    status: audit.status,
    integrity: audit.integrity,
    verified_summary: asNonEmptyString(audit.verified_summary, 'audit.verified_summary'),
    evidence,
    findings,
    remaining_gaps: asStringArray(audit.remaining_gaps ?? [], 'audit.remaining_gaps'),
    suggested_updates: suggestedUpdates,
    resolves_issue_ids: asStringArray(audit.resolves_issue_ids ?? [], 'audit.resolves_issue_ids'),
  }
}

export function validateManagerOutput(output) {
  if (!isObject(output)) throw new Error('manager output must be an object')
  assertAllowedKeys(output, ['decision', 'reason', 'state_updates', 'contract', 'question'], 'manager output')
  if (!MANAGER_DECISION_SET.has(output.decision)) {
    throw new Error(`invalid manager decision "${output.decision}"`)
  }
  const stateUpdates = (output.state_updates ?? []).map((update, index) => {
    if (!isObject(update)) throw new Error(`state_updates[${index}] must be an object`)
    assertAllowedKeys(update, ['audit_id', 'finding_id', 'record_id'], 'manager state update')
    const recordId = asNonEmptyString(update.record_id, 'state_update.record_id', 200)
    if (!isRecordId(recordId)) throw new Error(`invalid state_update.record_id "${recordId}"`)
    return {
      audit_id: asNonEmptyString(update.audit_id, 'state_update.audit_id', 200),
      finding_id: asNonEmptyString(update.finding_id, 'state_update.finding_id', 200),
      record_id: recordId,
    }
  })
  const updateKeys = stateUpdates.map(item => `${item.audit_id}/${item.finding_id}/${item.record_id}`)
  if (new Set(updateKeys).size !== updateKeys.length) throw new Error('duplicate manager state update')

  let contract = null
  let question = null
  if (output.decision === 'execute') {
    if (output.contract === undefined || output.contract === null) throw new Error('execute requires contract')
    contract = validateContract(output.contract)
  } else if (output.contract !== undefined && output.contract !== null) {
    throw new Error(`${output.decision} requires contract to be null`)
  }
  if (output.decision === 'ask') {
    question = asNonEmptyString(output.question, 'manager.question', 1000)
  } else if (output.question !== undefined && output.question !== null && output.question !== '') {
    throw new Error(`${output.decision} requires question to be null`)
  }
  return {
    decision: output.decision,
    reason: asNonEmptyString(output.reason, 'manager.reason', 2000),
    state_updates: stateUpdates,
    contract,
    question,
  }
}

function validateRecord(record, expectedType) {
  if (!isObject(record)) throw new Error(`${expectedType} record must be an object`)
  assertAllowedKeys(record, [
    'id', 'type', 'content', 'source', 'status', 'lifecycle', 'requirement_version',
    'scope', 'dependencies', 'evidence_refs', 'history', 'valid', 'invalid_reason',
  ], `${expectedType} record`)
  if (!RECORD_TYPES.has(record.type) || record.type !== expectedType) {
    throw new Error(`record type must be "${expectedType}"`)
  }
  const id = asNonEmptyString(record.id, 'record.id', 200)
  if (!id.startsWith(RECORD_PREFIX[expectedType])) throw new Error(`record id/type mismatch for "${id}"`)
  if (!RECORD_STATUS_SET.has(record.status)) throw new Error(`invalid record status "${record.status}"`)
  if (!RECORD_LIFECYCLES.has(record.lifecycle)) {
    throw new Error(`invalid record lifecycle "${record.lifecycle}"`)
  }
  if (expectedType !== 'requirement' && record.lifecycle !== 'active') {
    throw new Error(`${expectedType} records cannot be revoked`)
  }
  if (record.lifecycle === 'revoked' && record.valid) {
    throw new Error('a revoked requirement cannot be valid')
  }
  if (!Number.isInteger(record.requirement_version) || record.requirement_version < 1) {
    throw new Error('record.requirement_version must be positive integer')
  }
  if (typeof record.valid !== 'boolean') throw new Error('record.valid must be boolean')
  if (!record.valid && record.status === 'completed') throw new Error('an invalid record cannot be completed')
  if (!Array.isArray(record.history)) throw new Error('record.history must be an array')
  return {
    ...copy(record),
    id,
    content: asNonEmptyString(record.content, 'record.content'),
    source: asNonEmptyString(record.source, 'record.source', 1000),
    scope: validateScope(record.scope, 'record.scope'),
    dependencies: asStringArray(record.dependencies ?? [], 'record.dependencies'),
    evidence_refs: validateEvidenceRefs(record.evidence_refs ?? [], 'record.evidence_refs'),
  }
}

function validateIntegrityIssue(issue) {
  if (!isObject(issue)) throw new Error('integrity issue must be an object')
  assertAllowedKeys(issue, ['id', 'status', 'opened_by_audit', 'resolved_by_audit', 'summary'], 'integrity issue')
  if (!['open', 'resolved'].includes(issue.status)) throw new Error('invalid integrity issue status')
  const out = {
    id: asNonEmptyString(issue.id, 'integrity_issue.id', 200),
    status: issue.status,
    opened_by_audit: asNonEmptyString(issue.opened_by_audit, 'integrity_issue.opened_by_audit', 200),
    resolved_by_audit: issue.resolved_by_audit ?? null,
    summary: asNonEmptyString(issue.summary, 'integrity_issue.summary'),
  }
  if (out.status === 'resolved') {
    out.resolved_by_audit = asNonEmptyString(out.resolved_by_audit, 'integrity_issue.resolved_by_audit', 200)
  } else if (out.resolved_by_audit !== null) {
    throw new Error('open integrity issue cannot have resolved_by_audit')
  }
  return out
}

export function resolveEvidenceRef(state, ref) {
  const normalized = validateEvidenceRef(ref)
  const audit = (state.audit_history ?? []).find(item => item.id === normalized.audit_id)
  if (!audit) throw new Error(`unknown audit "${normalized.audit_id}"`)
  const evidence = audit.evidence.find(item => item.id === normalized.evidence_id)
  if (!evidence) throw new Error(`unknown evidence "${normalized.audit_id}/${normalized.evidence_id}"`)
  return { audit, evidence }
}

export function validateTaskState(state) {
  if (!isObject(state)) throw new Error('task state must be an object')
  assertAllowedKeys(state, [
    'schema', 'task', 'requirements', 'artifacts', 'facts', 'shopper_replies',
    'audit_history', 'integrity_issues', 'round', 'decision', 'contract', 'question',
  ], 'task state')
  if (state.schema !== TASK_STATE_SCHEMA) throw new Error(`unsupported state schema "${state.schema}"`)
  if (!isObject(state.task)) throw new Error('state.task must be an object')
  assertAllowedKeys(state.task, ['id', 'original_goal', 'persona'], 'state.task')
  asNonEmptyString(state.task.id, 'state.task.id', 200)
  asNonEmptyString(state.task.original_goal, 'state.task.original_goal')
  if (state.task.persona !== null && state.task.persona !== undefined
    && typeof state.task.persona !== 'string') throw new Error('state.task.persona must be string or null')
  if (!Number.isInteger(state.round) || state.round < 0) throw new Error('state.round must be non-negative integer')

  const shopperReplies = (state.shopper_replies ?? []).map((reply, index) => {
    if (!isObject(reply)) throw new Error(`shopper_replies[${index}] must be an object`)
    assertAllowedKeys(reply, ['id', 'question', 'reply', 'event_ref', 'recorded_at'], 'shopper reply')
    return {
      id: asNonEmptyString(reply.id, 'shopper_reply.id', 200),
      question: asNonEmptyString(reply.question, 'shopper_reply.question', 1000),
      reply: asNonEmptyString(reply.reply, 'shopper_reply.reply', 2000),
      event_ref: asNonEmptyString(reply.event_ref, 'shopper_reply.event_ref', 200),
      recorded_at: Number.isFinite(reply.recorded_at) ? reply.recorded_at : 0,
    }
  })
  if (new Set(shopperReplies.map(reply => reply.id)).size !== shopperReplies.length) {
    throw new Error('duplicate shopper reply id')
  }

  const requirements = (state.requirements ?? []).map(item => validateRecord(item, 'requirement'))
  const artifacts = (state.artifacts ?? []).map(item => validateRecord(item, 'artifact'))
  const facts = (state.facts ?? []).map(item => validateRecord(item, 'fact'))
  const records = [...requirements, ...artifacts, ...facts]
  const recordIds = records.map(item => item.id)
  if (new Set(recordIds).size !== recordIds.length) throw new Error('duplicate record id in task state')
  const recordIdSet = new Set(recordIds)
  for (const record of records) {
    for (const dependency of record.dependencies) {
      if (!recordIdSet.has(dependency)) throw new Error(`dangling dependency "${dependency}"`)
      if (dependency === record.id) throw new Error(`self dependency on "${record.id}"`)
    }
  }
  const visiting = new Set()
  const visited = new Set()
  const recordsById = new Map(records.map(item => [item.id, item]))
  function visit(id) {
    if (visiting.has(id)) throw new Error(`dependency cycle at "${id}"`)
    if (visited.has(id)) return
    visiting.add(id)
    for (const dependency of recordsById.get(id).dependencies) visit(dependency)
    visiting.delete(id)
    visited.add(id)
  }
  for (const id of recordIds) visit(id)

  const audits = (state.audit_history ?? []).map(validateAuditReport)
  const auditIds = audits.map(item => item.id)
  if (new Set(auditIds).size !== auditIds.length) throw new Error('duplicate audit id in task state')
  const knownAudits = new Map()
  for (const audit of audits) {
    const available = new Map(knownAudits)
    available.set(audit.id, audit)
    for (const finding of audit.findings) {
      for (const ref of finding.evidence_refs) {
        const owner = available.get(ref.audit_id)
        if (!owner || !owner.evidence.some(item => item.id === ref.evidence_id)) {
          throw new Error(`finding has unresolved evidence ref "${ref.audit_id}/${ref.evidence_id}"`)
        }
      }
    }
    knownAudits.set(audit.id, audit)
  }

  const normalizedState = { ...state, audit_history: audits }
  for (const record of records) {
    for (const ref of record.evidence_refs) {
      const { audit, evidence } = resolveEvidenceRef(normalizedState, ref)
      if (!evidenceCoversScope(record.scope, evidence.scope)) {
        throw new Error(`evidence scope does not cover record "${record.id}"`)
      }
      if (record.status === 'completed' && audit.integrity !== 'clean') {
        throw new Error(`completed record "${record.id}" cites non-clean audit`)
      }
    }
    if (record.status === 'completed' && record.evidence_refs.length === 0) {
      throw new Error(`completed record "${record.id}" lacks evidence`)
    }
    if (record.status === 'completed') {
      const supportedByFinding = audits.some(audit => audit.findings.some(finding => {
        if (finding.record_id !== record.id || !finding.supported
          || finding.proposed_status !== 'completed'
          || (record.type === 'requirement'
            && finding.requirement_version !== record.requirement_version)) return false
        const findingRefs = new Set(
          finding.evidence_refs.map(ref => `${ref.audit_id}/${ref.evidence_id}`),
        )
        return record.evidence_refs.every(ref =>
          findingRefs.has(`${ref.audit_id}/${ref.evidence_id}`))
      }))
      if (!supportedByFinding) {
        throw new Error(`completed record "${record.id}" lacks a matching audited finding`)
      }
    }
    if (record.status === 'completed') {
      const currentRefs = new Set(record.evidence_refs.map(
        ref => `${ref.audit_id}/${ref.evidence_id}`))
      const hasAuditedFinding = audits.some(audit => audit.findings.some(finding => {
        if (finding.record_id !== record.id || !finding.supported
          || finding.proposed_status !== 'completed'
          || (record.type === 'requirement'
            && finding.requirement_version !== record.requirement_version)) return false
        const findingRefs = finding.evidence_refs.map(
          ref => `${ref.audit_id}/${ref.evidence_id}`)
        return findingRefs.length === currentRefs.size
          && findingRefs.every(ref => currentRefs.has(ref))
      }))
      if (!hasAuditedFinding) {
        throw new Error(`completed record "${record.id}" lacks a matching audited finding`)
      }
    }
  }

  const integrityIssues = (state.integrity_issues ?? []).map(validateIntegrityIssue)
  const issueIds = integrityIssues.map(item => item.id)
  if (new Set(issueIds).size !== issueIds.length) throw new Error('duplicate integrity issue id')
  for (const issue of integrityIssues) {
    const opener = knownAudits.get(issue.opened_by_audit)
    if (!opener) throw new Error('integrity issue opener audit missing')
    if (opener.integrity !== 'violation') {
      throw new Error('integrity issue must be opened by a violation audit')
    }
    if (issue.resolved_by_audit) {
      const resolver = knownAudits.get(issue.resolved_by_audit)
      if (!resolver) throw new Error('integrity issue resolver audit missing')
      if (resolver.integrity !== 'clean'
        || !resolver.resolves_issue_ids.includes(issue.id)) {
        throw new Error('integrity issue resolution is not backed by its clean audit')
      }
    }
  }

  if (!isObject(state.decision) || !STATE_DECISION_SET.has(state.decision.kind)) {
    throw new Error('invalid state decision')
  }
  asNonEmptyString(state.decision.reason, 'state.decision.reason', 2000)
  let contract = null
  if (state.contract !== null && state.contract !== undefined) {
    contract = validateContract(state.contract)
    for (const id of [...contract.dependencies, ...contract.relevant_state_ids]) {
      if (!recordIdSet.has(id)) throw new Error(`contract references missing record "${id}"`)
    }
    for (const id of contract.relevant_audit_ids) {
      if (!knownAudits.has(id)) throw new Error(`contract references missing audit "${id}"`)
    }
  }
  if (state.decision.kind === 'execute' && contract === null) {
    throw new Error('execute state requires contract')
  }
  if (state.decision.kind !== 'execute' && contract !== null) {
    throw new Error('non-execute state cannot retain contract')
  }
  if (state.decision.kind === 'ask') {
    asNonEmptyString(state.question, 'state.question', 1000)
  } else if (state.question !== null && state.question !== undefined) {
    throw new Error('non-ask state cannot retain question')
  }
  return copy(state)
}

export function makeEvidence({
  id, kind = 'read_only_observation', summary, scope = null, observation_ref,
}) {
  return validateEvidenceBlock({ id, kind, summary, scope, observation_ref })
}

export function makeFinding({
  finding_id, record_id = null, requirement_version = null, criterion, supported,
  proposed_status = 'pending', evidence_refs = [], dependencies = [], summary,
  content = null, scope = null,
}) {
  return validateFinding({
    finding_id, record_id, requirement_version, criterion, supported, proposed_status,
    evidence_refs, dependencies, summary, content, scope,
  })
}
