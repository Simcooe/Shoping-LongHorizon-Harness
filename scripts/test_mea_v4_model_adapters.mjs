import assert from 'node:assert/strict'
import { AuditorModelAdapter, ManagerModelAdapter, jsonText } from '../src/mea-v4/model-adapters.js'

assert.deepEqual(jsonText('```json\n{"decision":"blocked"}\n```'), { decision: 'blocked' })
let bodies = []
const manager = new ManagerModelAdapter({ apiKey: 'test', fetchImpl: async (_url, options) => {
  bodies.push(JSON.parse(options.body))
  return { ok: true, async json() { return { choices: [{ message: { content: JSON.stringify({
    decision: 'execute', reason: 'advance', apply_findings: ['audit-1/finding-1'],
    contract: { goal: 'inspect candidate', acceptance_criteria: ['candidate inspected'],
      allowed_tools: ['search', 'click', 'buy', 'select'], suggested_tools: ['search'],
      max_tool_calls: 3, timeout_seconds: 60, deny_click_values: ['Buy Now'] }, question: null,
  }) } }], usage: { total_tokens: 1 } } } }
} })
const output = await manager.plan({
  available_tools: ['search', 'click', 'finish'],
  state: { requirements: [{ id: 'req-1', lifecycle: 'active' }] },
  prior_audits: [{ id: 'audit-1', findings: [{ finding_id: 'finding-1', record_id: 'req-1' }] }],
})
assert.equal(output.decision, 'execute')
assert.deepEqual(output.state_updates, [{ audit_id: 'audit-1', finding_id: 'finding-1', record_id: 'req-1' }])
assert.deepEqual(output.contract.role_tools, ['search', 'click'])
assert.deepEqual(output.contract.manager_suggested_tools, ['search'])
assert.equal(output.contract.id.startsWith('contract-'), true)
assert.deepEqual(output.contract.tool_rules, [{ tool: 'click', deny_values: ['Buy Now'] }])
assert.equal(bodies[0].response_format.type, 'json_object')

const auditor = new AuditorModelAdapter({ apiKey: 'test', fetchImpl: async () => ({ ok: true, async json() { return { choices: [{ message: { content: JSON.stringify({
  status: 'incomplete', integrity: 'clean', verified_summary: 'candidate visible',
  findings: [{ record_id: 'req-1', supported: true, status: 'pending', summary: 'purchase missing' }],
  remaining_gaps: ['terminal receipt missing'],
}) } }] } } }) })
const audited = await auditor.audit({})
assert.deepEqual(audited, {
  status: 'incomplete', integrity: 'clean', verified_summary: 'candidate visible',
  findings: [{ record_id: 'req-1', supported: true, proposed_status: 'pending',
    summary: 'purchase missing', content: null }],
  remaining_gaps: ['terminal receipt missing'], resolves_issue_ids: [],
})
console.log('test_mea_v4_model_adapters.mjs: all assertions passed')
