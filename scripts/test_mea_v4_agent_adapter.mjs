import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { AgentAdapter } from '../src/mea-v4/agent-adapter.js'
import { buildTaskTraces } from '../src/mea-v4/exporter.js'

const dir = mkdtempSync(join(tmpdir(), 'mea-v4-stage3-'))
const requests = []
const adapter = new AgentAdapter({ runDir: dir, backend: {
  async runEpisode(input) {
    requests.push(input)
    input.event('tool_call', { call_id: `c-${requests.length}`, tool_name: 'search', tool_arguments: { keywords: '阀' } })
    input.event('tool_result', { call_id: `c-${requests.length}`, raw_result: { done: false }, model_visible_text: 'page' })
    return { summary: 'found candidate' }
  },
} })
const context = {
  task_id: 'task-1', run_id: 'run-1', attempt_id: 'attempt-1', round_id: 'round-1',
  original_task: '买一个红色阀', task_state: { public: true }, prior_audits: [{ id: 'audit-1' }],
  tool_schemas: [{ name: 'search', parameters: {} }],
}
const firstContract = { id: 'contract-1', role_tools: ['search'] }
const secondContract = { id: 'contract-2', role_tools: ['search'] }
const first = await adapter.runEpisode('executor', context, firstContract, { env_idx: 1 }, { max_tool_calls: 2, timeout_ms: 1000 })
const second = await adapter.runEpisode('executor', { ...context, round_id: 'round-2', prior_audits: [{ id: 'audit-1', clean: true }] }, secondContract, { env_idx: 1 }, { max_tool_calls: 2, timeout_ms: 1000 })
assert.equal(first.runtime_status, 'completed')
assert.equal(second.runtime_status, 'completed')
assert.notEqual(first.episode_id, second.episode_id)
assert.equal(requests[0].input.task, '买一个红色阀')
assert.equal(requests[0].input.prior_audits[0].id, 'audit-1')
assert.equal(JSON.stringify(requests[0]).includes('clean'), false)

const base = { schema: 'longhorizon-task-journal-v2', task_id: 'task-1', run_id: 'run-1', attempt_id: 'attempt-1', round_id: 'round-1', episode_id: first.episode_id }
const events = [
  { ...base, event_id: 'call-a', sequence: 1, ts: 1, role: 'executor', source: 'executor', event_type: 'tool_call', local_step: 1, call_id: 'a', tool_call_id: 'ta', tool_name: 'search', tool_arguments: { keywords: '阀' } },
  { ...base, event_id: 'call-b', sequence: 2, ts: 2, role: 'executor', source: 'executor', event_type: 'tool_call', local_step: 1, call_id: 'b', tool_call_id: 'tb', tool_name: 'click', tool_arguments: { value: 'ASIN' } },
  { ...base, event_id: 'result-b', sequence: 3, ts: 3, role: 'executor', source: 'environment', event_type: 'tool_result', local_step: 1, call_id: 'b', raw_result: { done: false }, model_visible_text: 'candidate' },
  { ...base, event_id: 'result-a', sequence: 4, ts: 4, role: 'executor', source: 'environment', event_type: 'tool_result', local_step: 1, call_id: 'a', raw_result: { done: true, purchase: { asin: 'ASIN' } }, raw_done: true, env_done: true, model_visible_text: 'Episode finished.' },
  { ...base, event_id: 'qa', sequence: 5, ts: 5, role: 'controller', source: 'shopper', event_type: 'shopper_qa', manager_question: '颜色？', shopper_reply: '红色' },
]
const traces = buildTaskTraces(events, { task: '原始任务', id: 'task-1' })
assert.equal(traces.model_trace.task, '原始任务')
assert.equal(traces.model_trace.steps[0].call_id, 'a')
assert.equal(traces.raw_trace.steps[0].result_event_id, 'result-a')
assert.equal(traces.raw_trace.terminal.purchase.asin, 'ASIN')
assert.equal(traces.model_trace.shopper_qa[0].actor, 'controller')
assert.equal(traces.model_trace.steps.some(step => step.tool_name === 'ask_shopper'), true)
assert.equal(readFileSync(join(first.log_dir, 'episode.jsonl'), 'utf8').includes('audit-1'), true)
// Contract tool permissions and backend timeout are enforced by the adapter.
const denied = await adapter.runEpisode('executor', context,
  { id: 'contract-deny', role_tools: [] }, { env_idx: 1 },
  { max_tool_calls: 2, timeout_ms: 1000 })
assert.equal(denied.runtime_status, 'completed')
assert.deepEqual(requests.at(-1).input.tool_schemas, [])
const hanging = new AgentAdapter({ runDir: dir, backend: {
  runEpisode: () => new Promise(() => {}),
} })
const timeoutResult = await hanging.runEpisode('executor',
  { ...context, tool_schemas: [] }, { id: 'contract-timeout', role_tools: [] },
  { env_idx: 1 }, { timeout_ms: 10 })
assert.equal(timeoutResult.runtime_status, 'timeout')

rmSync(dir, { recursive: true, force: true })
console.log('test_mea_v4_agent_adapter.mjs: all assertions passed')
