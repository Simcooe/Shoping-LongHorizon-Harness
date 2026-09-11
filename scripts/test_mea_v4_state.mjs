// mea-v4 stages 1-2: schema, audit provenance and reducer invariants.
import assert from 'node:assert/strict'
import {
  TASK_STATE_SCHEMA,
  validateContract,
  validateTaskState,
  makeEvidence,
  makeFinding,
} from '../src/mea-v4/schema.js'
import { createObservationRegistry } from '../src/mea-v4/observation.js'
import {
  addRequirementFromReply,
  applyAuditReport,
  applyDecision,
  applyManagerOutput,
  cloneState,
  createTaskState,
  finalize,
  invalidateRequirements,
  modifyRequirementFromReply,
  recordShopperReply,
  revokeRequirementFromReply,
} from '../src/mea-v4/state.js'

const GOAL = '买红色手柄排气阀，价格30元左右。'
const ref = (auditId, evidenceId) => ({ audit_id: auditId, evidence_id: evidenceId })
const contract = (overrides = {}) => ({
  id: 'contract-1',
  goal: '核验候选',
  acceptance_criteria: ['核验颜色和价格'],
  boundary_constraints: [],
  dependencies: [],
  relevant_state_ids: [],
  relevant_audit_ids: [],
  role_tools: ['search', 'click'],
  manager_suggested_tools: ['click'],
  tool_rules: [{ tool: 'click', deny_values: ['Buy Now'] }],
  budget: { max_tool_calls: 5, timeout_seconds: 60 },
  ...overrides,
})
const evidence = (id = 'ev-1', scope = null) =>
  makeEvidence({
    id,
    summary: `evidence ${id}`,
    scope,
    observation_ref: {
      event_id: `evt-${id}`,
      snapshot_id: `snapshot-${id}`,
      environment_session: 'env-session-1',
    },
  })
const finding = ({
  id = 'finding-1',
  recordId = 'req-1',
  status = 'completed',
  auditId = 'audit-1',
  evidenceId = 'ev-1',
  supported = true,
  content = null,
  scope = null,
  requirementVersion = recordId.startsWith('req-') ? 1 : null,
} = {}) => makeFinding({
  finding_id: id,
  record_id: recordId,
  requirement_version: requirementVersion,
  criterion: 'criterion',
  supported,
  proposed_status: status,
  evidence_refs: status === 'completed' ? [ref(auditId, evidenceId)] : [],
  summary: 'finding summary',
  content,
  scope,
})
const audit = ({
  id = 'audit-1',
  round = 1,
  contractId = 'contract-1',
  status = 'complete',
  integrity = 'clean',
  evidenceItems = [evidence()],
  findings = [finding({ auditId: id })],
  resolves = [],
} = {}) => ({
  id,
  round,
  contract_id: contractId,
  status,
  integrity,
  verified_summary: 'audit summary',
  evidence: evidenceItems,
  findings,
  remaining_gaps: [],
  suggested_updates: findings
    .filter(item => item.record_id)
    .map(item => ({ finding_id: item.finding_id, record_id: item.record_id })),
  resolves_issue_ids: resolves,
})
const manager = ({
  decision = 'execute',
  updates = [],
  nextContract = contract(),
  question = null,
} = {}) => ({
  decision,
  reason: 'manager reason',
  state_updates: updates,
  contract: decision === 'execute' ? nextContract : null,
  question: decision === 'ask' ? (question ?? '请确认规格') : null,
})
const initial = (requirements = [{ text: '红色手柄' }]) =>
  createTaskState({ taskId: 'task-1', originalGoal: GOAL, initialRequirements: requirements })
const planned = (state = initial(), nextContract = contract()) =>
  applyManagerOutput(state, manager({ nextContract }))

// Initial state is valid and adding a reply cannot duplicate req-1.
{
  const state = initial()
  assert.equal(state.schema, TASK_STATE_SCHEMA)
  assert.equal(state.decision.kind, 'planning')
  const added = addRequirementFromReply(state, { text: '全铜材质' })
  assert.deepEqual(added.requirements.map(item => item.id), ['req-1', 'req-2'])
  assert.doesNotThrow(() => validateTaskState(added))
}

// Real shopper replies are retained independently from requirement derivation.
{
  const state = recordShopperReply(initial(), {
    id: 'reply-1',
    question: '需要什么颜色？',
    reply: '黑色',
    eventRef: 'evt-shopper-1',
    recordedAt: 1,
  })
  assert.equal(state.shopper_replies[0].reply, '黑色')
  assert.equal(state.requirements.length, 1)
  assert.throws(() => recordShopperReply(state, {
    id: 'reply-1', question: 'q', reply: 'r', eventRef: 'evt-2',
  }), /duplicate shopper reply/)
}

// Full-state validation rejects duplicate IDs, dangling dependencies and cycles.
{
  const duplicate = cloneState(initial())
  duplicate.facts.push({
    ...duplicate.requirements[0],
    type: 'fact',
    id: 'req-1',
  })
  assert.throws(() => validateTaskState(duplicate), /id\/type mismatch|duplicate record/)

  const dangling = cloneState(initial())
  dangling.requirements[0].dependencies = ['fact-missing']
  assert.throws(() => validateTaskState(dangling), /dangling dependency/)

  const cyclic = initial([{ text: 'a' }, { text: 'b' }])
  cyclic.requirements[0].dependencies = ['req-2']
  cyclic.requirements[1].dependencies = ['req-1']
  assert.throws(() => validateTaskState(cyclic), /dependency cycle/)
}

// Audit evidence can be checked against a runtime-owned read-only observation registry.
{
  const registry = createObservationRegistry([{
    event_id: 'evt-ev-1',
    snapshot_id: 'snapshot-ev-1',
    environment_session: 'env-session-1',
    read_only: true,
    source: 'environment.inspect',
    scope: null,
    raw_ref: 'audit/raw/evt-ev-1.json',
  }])
  assert.doesNotThrow(() => applyAuditReport(planned(), audit(), {
    resolveObservation: refValue => registry.resolve(refValue),
  }))
  assert.throws(() => applyAuditReport(planned(), audit(), {
    resolveObservation: () => { throw new Error('unknown observation') },
  }), /unknown observation/)
  assert.throws(() => createObservationRegistry([{
    event_id: 'bad', snapshot_id: 's', environment_session: 'env',
    read_only: false, source: 'executor', raw_ref: 'raw.json',
  }]), /must be read-only/)
}

// Audit append records evidence but does not mutate task records.
{
  const state = planned()
  const withAudit = applyAuditReport(state, audit())
  assert.equal(withAudit.audit_history.length, 1)
  assert.equal(withAudit.requirements[0].status, 'pending')
}

// Manager can only select an existing audited finding.
{
  const state = applyAuditReport(planned(), audit())
  const updated = applyManagerOutput(state, manager({
    updates: [{ audit_id: 'audit-1', finding_id: 'finding-1', record_id: 'req-1' }],
    nextContract: contract({ id: 'contract-2', relevant_state_ids: ['req-1'], relevant_audit_ids: ['audit-1'] }),
  }))
  assert.equal(updated.requirements[0].status, 'completed')
  assert.deepEqual(updated.requirements[0].evidence_refs, [ref('audit-1', 'ev-1')])
  assert.throws(() => applyManagerOutput(initial(), manager({
    updates: [{ audit_id: 'fake', finding_id: 'fake', record_id: 'fact-1' }],
  })), /unknown audit/)
}

// A new fact must come from a supported finding and keeps audit scope/provenance.
{
  const scopedEvidence = evidence('ev-price', { asin: 'A1' })
  const factFinding = finding({
    id: 'finding-price',
    recordId: 'fact-price',
    auditId: 'audit-price',
    evidenceId: 'ev-price',
    content: '价格30元',
    scope: { asin: 'A1' },
  })
  const state = applyAuditReport(planned(), audit({
    id: 'audit-price',
    evidenceItems: [scopedEvidence],
    findings: [factFinding],
  }))
  const updated = applyManagerOutput(state, manager({
    updates: [{
      audit_id: 'audit-price',
      finding_id: 'finding-price',
      record_id: 'fact-price',
    }],
    nextContract: contract({ id: 'contract-2', relevant_audit_ids: ['audit-price'] }),
  }))
  assert.equal(updated.facts[0].source, 'audit:audit-price/finding-price')
  assert.deepEqual(updated.facts[0].scope, { asin: 'A1' })
  assert.throws(() => validateTaskState({
    ...updated,
    facts: [{ ...updated.facts[0], scope: { asin: 'B1' } }],
  }), /evidence scope/)
}

// Bare Manager fields are rejected; Manager cannot rewrite a requirement.
{
  const state = planned()
  assert.throws(() => applyManagerOutput(state, {
    decision: 'execute',
    reason: 'x',
    state_updates: [{
      record_id: 'fact-1',
      status: 'completed',
      evidence_refs: ['fake'],
      content: 'invented',
    }],
    contract: contract(),
  }), /unknown manager state update field/)

  const rewriteFinding = finding({
    auditId: 'audit-rewrite',
    content: '黑色手柄',
  })
  const audited = applyAuditReport(state, audit({
    id: 'audit-rewrite',
    findings: [rewriteFinding],
  }))
  assert.throws(() => applyManagerOutput(audited, manager({
    updates: [{
      audit_id: 'audit-rewrite',
      finding_id: 'finding-1',
      record_id: 'req-1',
    }],
  })), /rewrite requirement content/)
}

// incomplete + clean may preserve a specifically verified fact.
{
  const state = applyAuditReport(planned(), audit({
    status: 'incomplete',
    findings: [finding({ auditId: 'audit-1' })],
  }))
  const updated = applyManagerOutput(state, manager({
    updates: [{ audit_id: 'audit-1', finding_id: 'finding-1', record_id: 'req-1' }],
    nextContract: contract({ id: 'contract-2', relevant_audit_ids: ['audit-1'] }),
  }))
  assert.equal(updated.requirements[0].status, 'completed')
}

// Audited facts can establish dependency edges used by later invalidation.
{
  const dependentFinding = finding({
    id: 'finding-dependent',
    recordId: 'fact-dependent',
    auditId: 'audit-dependent',
    content: 'candidate matches color requirement',
    requirementVersion: null,
  })
  dependentFinding.dependencies = ['req-1']
  let state = applyAuditReport(planned(), audit({
    id: 'audit-dependent',
    findings: [dependentFinding],
  }))
  state = applyManagerOutput(state, manager({
    updates: [{
      audit_id: 'audit-dependent', finding_id: 'finding-dependent', record_id: 'fact-dependent',
    }],
    nextContract: contract({ id: 'contract-2', relevant_audit_ids: ['audit-dependent'] }),
  }))
  assert.deepEqual(state.facts[0].dependencies, ['req-1'])
  const modified = modifyRequirementFromReply(state, { id: 'req-1', text: '黑色手柄' })
  assert.equal(modified.facts[0].valid, false)
}

// Requirement modification/revocation preserves history and invalidates dependents.
{
  const state = initial()
  state.facts.push({
    id: 'fact-dependent',
    type: 'fact',
    content: 'derived',
    source: 'test fixture',
    status: 'pending',
    lifecycle: 'active',
    requirement_version: 1,
    scope: null,
    dependencies: ['req-1'],
    evidence_refs: [],
    history: [],
    valid: true,
    invalid_reason: null,
  })
  validateTaskState(state)
  const modified = modifyRequirementFromReply(state, { id: 'req-1', text: '黑色手柄' })
  assert.equal(modified.requirements[0].requirement_version, 2)
  assert.equal(modified.requirements[0].history[0].content, '红色手柄')
  assert.equal(modified.facts[0].valid, false)
  const revoked = revokeRequirementFromReply(modified, { id: 'req-1' })
  assert.equal(revoked.requirements[0].valid, false)
}

// Scope invalidation affects pending as well as completed records.
{
  const state = initial()
  state.facts.push({
    id: 'fact-A',
    type: 'fact',
    content: 'candidate A',
    source: 'test fixture',
    status: 'pending',
    lifecycle: 'active',
    requirement_version: 1,
    scope: { asin: 'A1', option: 'red' },
    dependencies: [],
    evidence_refs: [],
    history: [],
    valid: true,
    invalid_reason: null,
  })
  validateTaskState(state)
  const invalidated = invalidateRequirements(state, {
    scope: { asin: 'A1' },
    reason: 'switched product',
  })
  assert.equal(invalidated.facts[0].valid, false)
}

// A finding is bound to the audited requirement version and cannot complete a later rewrite.
{
  let state = applyAuditReport(planned(), audit())
  state = modifyRequirementFromReply(state, { id: 'req-1', text: '黑色手柄' })
  assert.throws(() => applyManagerOutput(state, manager({
    decision: 'done',
    updates: [{ audit_id: 'audit-1', finding_id: 'finding-1', record_id: 'req-1' }],
  })), /stale requirement version/)
}

// Evidence invalidation keeps requirements active; only an explicit shopper revocation removes one.
{
  const audited = applyAuditReport(planned(), audit())
  const invalidated = invalidateRequirements(audited, {
    recordIds: ['req-1'],
    reason: 'selection changed',
  })
  assert.equal(invalidated.requirements[0].lifecycle, 'active')
  assert.equal(invalidated.requirements[0].status, 'untrusted')
  assert.equal(finalize(invalidated).ok, false)

  const revoked = revokeRequirementFromReply(audited, { id: 'req-1' })
  assert.equal(revoked.requirements[0].lifecycle, 'revoked')
  assert.equal(finalize(revoked).ok, true)
}

// done is rejected with pending or blocked requirements.
{
  assert.throws(() => applyManagerOutput(initial(), manager({
    decision: 'done',
    updates: [],
  })), /manager done rejected/)

  const blockedFinding = finding({
    status: 'blocked',
    supported: true,
    auditId: 'audit-blocked',
  })
  let state = applyAuditReport(planned(), audit({
    id: 'audit-blocked',
    findings: [blockedFinding],
  }))
  state = applyManagerOutput(state, manager({
    updates: [{
      audit_id: 'audit-blocked',
      finding_id: 'finding-1',
      record_id: 'req-1',
    }],
    nextContract: contract({ id: 'contract-2', relevant_audit_ids: ['audit-blocked'] }),
  }))
  assert.equal(finalize(state).ok, false)
  assert.throws(() => applyDecision(state, { kind: 'done', reason: 'x' }), /blocked requirements/)
}

// A valid done requires a complete clean final audit and completed requirements.
{
  let state = applyAuditReport(planned(), audit())
  state = applyManagerOutput(state, manager({
    decision: 'done',
    updates: [{ audit_id: 'audit-1', finding_id: 'finding-1', record_id: 'req-1' }],
  }))
  assert.equal(state.decision.kind, 'done')
  assert.equal(finalize(state).ok, true)
}

// Integrity issues can be explicitly resolved by a later clean audit.
{
  const state0 = planned(initial([]))
  const state1 = applyAuditReport(state0, audit({
    id: 'audit-bad',
    integrity: 'violation',
    findings: [],
    evidenceItems: [],
  }))
  assert.equal(finalize(state1).ok, false)
  const state1Planned = planned(state1, contract({ id: 'contract-2' }))
  const state2 = applyAuditReport(state1Planned, audit({
    id: 'audit-fixed',
    round: 2,
    contractId: 'contract-2',
    findings: [],
    evidenceItems: [],
    resolves: ['issue-audit-bad'],
  }))
  assert.equal(state2.integrity_issues[0].status, 'resolved')
  assert.equal(finalize(state2).ok, true)
}

// Contract validation rejects dangling/duplicate capabilities.
{
  const valid = validateContract(contract())
  assert.equal(valid.role_tools.length, 2)
  assert.throws(() => validateContract(contract({ role_tools: ['search', 'search'] })), /duplicate/)
  assert.throws(() => applyManagerOutput(initial(), manager({
    nextContract: contract({ relevant_state_ids: ['fact-missing'] }),
  })), /contract references missing record/)
}

console.log('test_mea_v4_state.mjs: all assertions passed')
