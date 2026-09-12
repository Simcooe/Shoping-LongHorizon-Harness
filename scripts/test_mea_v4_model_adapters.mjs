import assert from 'node:assert/strict'
import { AuditorModelAdapter, JsonChatRole, ManagerModelAdapter, jsonText } from '../src/mea-v4/model-adapters.js'

assert.deepEqual(jsonText('```json\n{"decision":"blocked"}\n```'), { decision: 'blocked' })
let bodies = []
const fetchImpl = async (_url, options) => {
  bodies.push(JSON.parse(options.body))
  return { ok: true, async json() { return { choices: [{ message: { content: '{"decision":"blocked","reason":"x","state_updates":[],"contract":null,"question":null}' } }], usage: { total_tokens: 1 } } } }
}
const manager = new ManagerModelAdapter({ apiKey: 'test', fetchImpl })
const output = await manager.plan({ task: { id: 't' } })
assert.equal(output.decision, 'blocked')
assert.equal(bodies[0].messages.length, 2)
assert.equal(bodies[0].messages[0].role, 'system')

const managerNormalized = new ManagerModelAdapter({ apiKey: 'test', fetchImpl: async () => ({ ok: true, async json() { return { choices: [{ message: { content: JSON.stringify({
  decision: 'execute', reason: 'x', state_updates: [], question: null,
  contract: { id: 'c', tool_rules: [{ tool: 'click', allow_values: ['product'] }] },
}) } }] } } }) })
const normalized = await managerNormalized.plan({})
assert.deepEqual(normalized.contract.tool_rules, [{ tool: 'click', allow_kinds: ['product'] }])

const managerAllowArray = new ManagerModelAdapter({ apiKey: 'test', fetchImpl: async () => ({ ok: true, async json() { return { choices: [{ message: { content: JSON.stringify({
  decision: 'execute', reason: 'x', state_updates: [], question: null,
  contract: { id: 'c', tool_rules: [{ tool: 'click', allow: ['Buy Now'] }] },
}) } }] } } }) })
const allowArray = await managerAllowArray.plan({})
assert.deepEqual(allowArray.contract.tool_rules, [{ tool: 'click', allow_kinds: ['Buy Now'] }])

const auditor = new AuditorModelAdapter({ apiKey: 'test', fetchImpl: async () => ({ ok: true, async json() { return { choices: [{ message: { content: JSON.stringify({
  status: 'incomplete', integrity: 'clean', verified_summary: 'x', evidence: [],
  findings: [{ evidence_refs: [{ audit_id: 'RUNTIME_AUDIT_ID', evidence_id: 'ev' }] }],
  remaining_gaps: ['x'], suggested_updates: [], resolves_issue_ids: [],
}) } }] } } }) })
const audited = await auditor.audit({ auditId: 'audit-real' })
assert.equal(audited.findings[0].evidence_refs[0].audit_id, 'audit-real')
assert.equal(audited.findings[0].proposed_status, 'pending')
const conflictAuditor = new AuditorModelAdapter({ apiKey: 'test', fetchImpl: async () => ({ ok: true, async json() { return { choices: [{ message: { content: JSON.stringify({
  status: 'complete', integrity: 'clean', verified_summary: 'receipt', evidence: [],
  findings: [{ finding_id: 'f1', record_id: 'req-1', supported: true,
    proposed_status: 'completed', evidence_refs: [], summary: 'done' }],
  remaining_gaps: [], suggested_updates: [{ finding_id: 'f1', record_id: 'req-1' }],
  resolves_issue_ids: [],
}) } }] } } }) })
const conflict = await conflictAuditor.audit({ auditId: 'audit-conflict',
  task: '需要精品全钢，1个',
  task_state: { requirements: [{ id: 'req-1', requirement_version: 1, content: '需要精品全钢，1个' }], shopper_replies: [] },
  evidence: { terminal_receipt: { asin: 'A1', options: { 型号: '普通全钢', 数量: '1个' } } },
})
assert.equal(conflict.status, 'incomplete')
assert.equal(conflict.findings[0].supported, false)
assert.equal(conflict.findings[0].proposed_status, 'pending')
assert.match(conflict.remaining_gaps.join(' '), /精品全钢/)

console.log('test_mea_v4_model_adapters.mjs: all assertions passed')
