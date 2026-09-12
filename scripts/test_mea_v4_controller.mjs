import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { createMeaController } from '../src/mea-v4/controller.js'

const dir = mkdtempSync(join(tmpdir(), 'mea-v4-stage5-'))
let managerCall = 0
const contract = {
  id: 'contract-1', goal: '核验并购买', acceptance_criteria: ['完成原始需求'],
  boundary_constraints: [], dependencies: [], relevant_state_ids: [], relevant_audit_ids: [],
  role_tools: ['search', 'click'], manager_suggested_tools: ['search'], tool_rules: [],
  budget: { max_tool_calls: 3, timeout_seconds: 30 },
}
const manager = { async plan(input) {
  managerCall += 1
  if (managerCall === 1) return { decision: 'execute', reason: '需要执行', state_updates: [], contract, question: null }
  if (managerCall === 2) return {
    decision: 'done', reason: '完成', state_updates: [{ audit_id: 'audit-1', finding_id: 'finding-1', record_id: 'req-1' }], contract: null, question: null,
  }
  return { decision: 'blocked', reason: 'unexpected', state_updates: [], contract: null, question: null }
} }
const executor = { async runEpisode(role, context) {
  assert.equal(role, 'executor')
  assert.equal(context.original_task, '买红色阀')
  return { episode_id: `episode-${context.round_id}`, runtime_status: 'completed',
    report: { summary: '完成公开操作', environment_done: true, tool_calls: 1 } }
} }
const auditor = { async audit({ taskContext, contract: active }) {
  assert.equal(taskContext.original_task, '买红色阀')
  return { audit: {
    schema: 'longhorizon-audit-v3', id: 'audit-1', round: 1, contract_id: active.id,
    status: 'complete', integrity: 'clean', verified_summary: '公开证据支持需求完成',
    evidence: [{ id: 'evidence-1', kind: 'read_only_environment_state', summary: '公开页面', scope: null,
      observation_ref: { event_id: 'inspection-1', snapshot_id: 'snapshot-1', environment_session: 'env-1' } }],
    findings: [{ finding_id: 'finding-1', record_id: 'req-1', requirement_version: 1, criterion: '完成原始需求', supported: true, proposed_status: 'completed',
      evidence_refs: [{ audit_id: 'audit-1', evidence_id: 'evidence-1' }], dependencies: [], summary: '已完成', content: null, scope: null }],
    remaining_gaps: [], suggested_updates: [{ finding_id: 'finding-1', record_id: 'req-1' }], resolves_issue_ids: [],
  }, evidence: { terminal_receipt: { asin: 'A1' } },
  observation_registry: { resolve: () => ({ read_only: true }) } }
} }
const result = await createMeaController({
  task: { id: 'task-1', original_goal: '买红色阀' }, runId: 'run-1', attemptId: 'attempt-1',
  environmentHandle: { environment_session: 'env-1', environment_lease: 'lease-1', tool_schemas: [] },
  manager, executor, auditor, journalPath: join(dir, 'journal.jsonl'), budget: { max_rounds: 2 },
}).run()
assert.equal(result.harness_outcome, 'audited_success')
assert.equal(result.runtime_status, 'completed')
assert.equal(result.episodes.length, 1)
assert.equal(result.audits.length, 1)
assert.equal(result.state.requirements[0].status, 'completed')
assert.equal(result.environment_done, true)
assert.equal(result.final_receipt_verified, true)
assert.equal(result.task_success, true)
assert.equal(readFileSync(join(dir, 'journal.jsonl'), 'utf8').includes('audit_report'), true)
// Two execute/audit rounds use increasing numeric audit rounds.
{
  let calls = 0
  const contracts = [contract, { ...contract, id: 'contract-2' }]
  const twoRounds = await createMeaController({
    task: { id: 'task-two', original_goal: '核验阀' }, runId: 'run-two', attemptId: 'attempt-two',
    environmentHandle: { tool_schemas: [] },
    manager: { async plan() {
      calls += 1
      if (calls <= 2) return { decision: 'execute', reason: 'next', state_updates: [], contract: contracts[calls - 1], question: null }
      return { decision: 'blocked', reason: 'fixture stop', state_updates: [], contract: null, question: null }
    } },
    executor: { async runEpisode(_role, context) {
      return { episode_id: `episode-${context.round}`, runtime_status: 'completed', report: { tool_calls: 0 } }
    } },
    auditor: { async audit({ taskContext, contract: active }) { return { audit: {
      schema: 'longhorizon-audit-v3', id: `audit-${taskContext.round}`, round: taskContext.round,
      contract_id: active.id, status: 'incomplete', integrity: 'clean', verified_summary: 'partial',
      evidence: [], findings: [], remaining_gaps: ['more'], suggested_updates: [], resolves_issue_ids: [],
    } } } },
    budget: { max_rounds: 3 },
  }).run()
  assert.equal(twoRounds.runtime_status, 'completed')
  assert.deepEqual(twoRounds.audits.map(audit => audit.round), [1, 2])
}

// A normal non-purchase environment terminal is a completed runtime but failed task.
{
  let calls = 0
  const terminalFailure = await createMeaController({
    task: { id: 'task-terminal-failure', original_goal: '买阀' },
    runId: 'run-terminal-failure', attemptId: 'attempt-terminal-failure',
    environmentHandle: { tool_schemas: [] },
    manager: { async plan() {
      calls += 1
      return { decision: 'execute', reason: 'try again', state_updates: [], contract, question: null }
    } },
    executor: { async runEpisode() { return {
      episode_id: 'episode-terminal', runtime_status: 'completed',
      report: { tool_calls: 1, environment_done: true },
    } } },
    auditor: { async audit() { return { audit: {
      schema: 'longhorizon-audit-v3', id: 'audit-terminal', round: 1,
      contract_id: contract.id, status: 'incomplete', integrity: 'clean',
      verified_summary: '环境因repeat_loop终止，任务未完成', evidence: [], findings: [],
      remaining_gaps: ['no purchase'], suggested_updates: [], resolves_issue_ids: [],
    }, evidence: { terminal_receipt: null } } } },
  }).run()
  assert.equal(terminalFailure.runtime_status, 'completed')
  assert.equal(terminalFailure.harness_outcome, 'environment_terminated_unresolved')
  assert.equal(terminalFailure.task_success, false)
  assert.equal(terminalFailure.environment_done, true)
  assert.equal(calls, 2)
}

// A terminal wrong-purchase followed by an invalid Manager done is a completed runtime/task failure.
{
  let calls = 0
  const wrongDone = await createMeaController({
    task: { id: 'task-wrong-done', original_goal: '买精品全钢' },
    runId: 'run-wrong-done', attemptId: 'attempt-wrong-done',
    environmentHandle: { tool_schemas: [] },
    manager: { async plan() {
      calls += 1
      if (calls === 1) return { decision: 'execute', reason: 'buy', state_updates: [], contract, question: null }
      return { decision: 'done', reason: 'incorrect success', state_updates: [], contract: null, question: null }
    } },
    executor: { async runEpisode() { return { episode_id: 'episode-wrong', runtime_status: 'completed', report: { tool_calls: 1, environment_done: true } } } },
    auditor: { async audit() { return { audit: {
      schema: 'longhorizon-audit-v3', id: 'audit-wrong', round: 1, contract_id: contract.id,
      status: 'incomplete', integrity: 'clean', verified_summary: 'wrong option', evidence: [], findings: [],
      remaining_gaps: ['精品全钢未满足'], suggested_updates: [], resolves_issue_ids: [],
    }, evidence: { terminal_receipt: { asin: 'A1' } } } } },
  }).run()
  assert.equal(wrongDone.runtime_status, 'completed')
  assert.equal(wrongDone.harness_outcome, 'environment_terminated_unresolved')
  assert.equal(wrongDone.task_success, false)
}

// Ask stores the real reply without inventing a requirement and replans without an Executor.
{
  let calls = 0
  const askResult = await createMeaController({
    task: { id: 'task-ask', original_goal: '买阀' }, runId: 'run-ask', attemptId: 'attempt-ask',
    environmentHandle: { shopper_session: 'shopper-ask', tool_schemas: [] },
    manager: { async plan(input) {
      calls += 1
      if (calls === 1) return { decision: 'ask', reason: '缺颜色', state_updates: [], contract: null, question: '需要什么颜色？' }
      assert.equal(input.state.shopper_replies[0].reply, '红色')
      return { decision: 'blocked', reason: 'fixture stop', state_updates: [], contract: null, question: null }
    } },
    shopper: { async ask({ session }) { assert.equal(session, 'shopper-ask'); return { reply: '红色' } } },
    executor: { async runEpisode() { throw new Error('ask must not execute') } },
    auditor: { async audit() { throw new Error('ask must not audit') } },
    budget: { max_manager_calls: 3 },
  }).run()
  assert.equal(askResult.runtime_status, 'completed')
  assert.equal(askResult.harness_outcome, 'blocked')
  assert.equal(askResult.ask_count, 1)
  assert.equal(askResult.episodes.length, 0)
  assert.equal(askResult.state.requirements.length, 1)
}

// A complete clean page audit without a terminal receipt cannot become audited_success.
{
  let calls = 0
  const noPurchase = await createMeaController({
    task: { id: 'task-no-buy', original_goal: '买阀' }, runId: 'run-no-buy', attemptId: 'attempt-no-buy',
    environmentHandle: { tool_schemas: [] },
    manager: { async plan() {
      calls += 1
      if (calls === 1) return { decision: 'execute', reason: 'inspect', state_updates: [], contract, question: null }
      return { decision: 'done', reason: 'incorrect done', state_updates: [{ audit_id: 'audit-page', finding_id: 'finding-page', record_id: 'req-1' }], contract: null, question: null }
    } },
    executor: { async runEpisode() { return { episode_id: 'episode-page', runtime_status: 'completed', report: { tool_calls: 0 } } } },
    auditor: { async audit() { return { audit: {
      schema: 'longhorizon-audit-v3', id: 'audit-page', round: 1, contract_id: contract.id,
      status: 'complete', integrity: 'clean', verified_summary: 'page only',
      evidence: [{ id: 'ev-page', kind: 'read_only_environment_state', summary: 'page', scope: null,
        observation_ref: { event_id: 'inspection-page', snapshot_id: 'snapshot-page', environment_session: 'env-1' } }],
      findings: [{ finding_id: 'finding-page', record_id: 'req-1', requirement_version: 1,
        criterion: 'x', supported: true, proposed_status: 'completed', evidence_refs: [{ audit_id: 'audit-page', evidence_id: 'ev-page' }],
        dependencies: [], summary: 'page', content: null, scope: null }], remaining_gaps: [],
      suggested_updates: [{ finding_id: 'finding-page', record_id: 'req-1' }], resolves_issue_ids: [],
    }, evidence: { terminal_receipt: null },
    observation_registry: { resolve: () => ({ read_only: true }) } } } },
  }).run()
  assert.equal(noPurchase.harness_outcome, 'unresolved')
  assert.equal(noPurchase.runtime_status, 'failed')
  assert.equal(noPurchase.final_receipt_verified, false)
}

rmSync(dir, { recursive: true, force: true })
console.log('test_mea_v4_controller.mjs: all assertions passed')
