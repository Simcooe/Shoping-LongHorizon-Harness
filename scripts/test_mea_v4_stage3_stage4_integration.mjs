// Stage 3 -> Stage 4 -> Stage 2 reducer integration fixture.
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { AgentAdapter } from '../src/mea-v4/agent-adapter.js'
import { AuditorAdapter, ReadOnlyInspector } from '../src/mea-v4/auditor-adapter.js'
import { applyAuditReport, applyManagerOutput, createTaskState } from '../src/mea-v4/state.js'
import { validateAuditReport } from '../src/mea-v4/schema.js'

const dir = mkdtempSync(join(tmpdir(), 'mea-v4-stage34-'))
const contract = {
  id: 'contract-1', goal: '核验红色候选', acceptance_criteria: ['颜色为红色'],
  boundary_constraints: [], dependencies: [], relevant_state_ids: ['req-1'],
  relevant_audit_ids: [], role_tools: ['search', 'click'],
  manager_suggested_tools: ['search'],
  tool_rules: [{ tool: 'click', deny_values: ['Buy Now'] }],
  budget: { max_tool_calls: 2, timeout_seconds: 10 },
}
let state = createTaskState({
  taskId: 'task-1', originalGoal: '买红色阀', initialRequirements: [{ text: '颜色为红色' }],
})
state = applyManagerOutput(state, {
  decision: 'execute', reason: 'inspect candidate', state_updates: [], contract, question: null,
})

const executor = new AgentAdapter({ runDir: dir, backend: {
  async runEpisode(input) {
    input.event('tool_call', {
      call_id: 'call-1', tool_name: 'search', tool_arguments: { keywords: '阀' },
    })
    input.event('tool_result', {
      call_id: 'call-1', raw_result: { done: false }, model_visible_text: '候选A1',
    })
    // Deliberately untrusted claim; the Auditor must rely on inspect instead.
    return { summary: '我认为候选是蓝色', output: { claimed_color: 'blue' } }
  },
} })
const episode = await executor.runEpisode('executor', {
  task_id: 'task-1', run_id: 'run-1', attempt_id: 'attempt-1', round_id: 'round-1',
  original_task: '买红色阀', task_state: state,
  tool_schemas: [{ name: 'search', parameters: {} }, { name: 'click', parameters: {} }],
}, contract, { env_idx: 1 }, { max_tool_calls: 2, timeout_ms: 1000 })
assert.equal(episode.runtime_status, 'completed')
assert.equal(episode.report.output.claimed_color, 'blue')

const inspector = new ReadOnlyInspector({ inspect: async () => ({
  read_only: true, source: 'fixture.inspect',
  observation_state: {
    observation_version: 'shopping-observation-v2', page_type: 'product_detail',
    product: { asin: 'A1', title: '阀', price: 30 }, selected_options: { color: 'red' },
    selected_price: 30, actions: ['Buy Now'],
  },
}) })
const auditor = new AuditorAdapter({ inspector, runDir: dir, auditor: async () => ({
  status: 'incomplete', integrity: 'clean', verified_summary: '只读快照显示红色；Executor蓝色声明不可信。',
  findings: [{ record_id: 'req-1', supported: true, status: 'completed',
    summary: '独立环境快照支持红色要求' }],
  remaining_gaps: ['尚未成交'],
}) })
const audited = await auditor.audit({
  taskContext: { task_id: 'task-1', round: 1, original_task: '买红色阀', task_state: state },
  contract, executorReport: episode.report,
  environmentHandle: { env_idx: 1, environment_session: 'env-1', environment_lease: 'lease-1' },
})
assert.doesNotThrow(() => validateAuditReport(audited.audit))
state = applyAuditReport(state, audited.audit, {
  resolveObservation: ref => audited.observation_registry.resolve(ref),
})
const runtimeFinding = audited.audit.findings[0]
state = applyManagerOutput(state, {
  decision: 'blocked', reason: 'candidate fact saved; purchase remains',
  state_updates: [{ audit_id: audited.audit.id, finding_id: runtimeFinding.finding_id, record_id: 'req-1' }],
  contract: null, question: null,
})
assert.equal(state.requirements[0].status, 'completed')
assert.equal(state.audit_history[0].status, 'incomplete')
assert.match(state.audit_history[0].verified_summary, /Executor蓝色声明不可信/)

rmSync(dir, { recursive: true, force: true })
console.log('test_mea_v4_stage3_stage4_integration.mjs: all assertions passed')
