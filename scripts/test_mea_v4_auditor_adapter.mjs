import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { validateAuditReport } from '../src/mea-v4/schema.js'
import { AuditorAdapter, ReadOnlyInspector } from '../src/mea-v4/auditor-adapter.js'

const dir = mkdtempSync(join(tmpdir(), 'mea-v4-stage4-'))
let calls = 0
const inspector = new ReadOnlyInspector({ inspect: async payload => {
  calls += 1
  assert.equal(payload.scope, 'current')
  return {
    read_only: true, source: 'mock.inspect', state_digest: 'same',
    observation_state: {
      observation_version: 'shopping-observation-v2', page_type: 'product_detail',
      product: { asin: 'A1', title: '阀', secret: 'should-not-export' },
      selected_options: { color: 'red' }, selected_price: 30,
    },
    receipt: { asin: 'A1', options: { color: 'red' }, amount: 30, quantity: 1 },
  }
}, logPath: join(dir, 'inspection.jsonl') })
const adapter = new AuditorAdapter({ inspector, runDir: dir })
const result = await adapter.audit({
  taskContext: { task_id: 'task-1', round: 1, original_task: '买红色阀' },
  contract: { id: 'contract-1' }, environmentHandle: {
    env_idx: 1, environment_session: 'env-1', environment_lease: 'lease-1',
  }, executorReport: { summary: '买到了蓝色' },
})
assert.equal(calls, 1)
assert.equal(result.audit.status, 'complete')
assert.equal(result.audit.integrity, 'clean')
assert.doesNotThrow(() => validateAuditReport(result.audit))
assert.equal(result.evidence.public_snapshot.product.asin, 'A1')
assert.equal('secret' in result.evidence.public_snapshot.product, false)
assert.equal(result.evidence.terminal_receipt.amount, 30)
assert.equal(readFileSync(join(dir, 'inspection.jsonl'), 'utf8').length > 0, true)
await assert.rejects(() => inspector.inspect({ env_idx: 1, environment_session: 'env-1' }), /environment_lease is required/)

// Empty receipts are unknown, not completed purchases.
const noReceipt = new AuditorAdapter({ inspector: new ReadOnlyInspector({ inspect: async () => ({
  read_only: true, observation_state: { page_type: 'done' }, receipt: {},
}) }) })
const incomplete = await noReceipt.audit({
  taskContext: { round: 1 }, contract: { id: 'contract-2' },
  environmentHandle: { env_idx: 1, environment_session: 'env-1', environment_lease: 'lease-1' },
  executorReport: {},
})
assert.equal(incomplete.audit.status, 'incomplete')
assert.equal(incomplete.evidence.receipt_status, 'unknown')

// Model output cannot override runtime-owned audit identity.
const guarded = new AuditorAdapter({ inspector, auditor: async ({ observation_ref }) => ({
  schema: 'bad', id: 'forged', round: 99, contract_id: 'forged',
  status: 'incomplete', integrity: 'clean', verified_summary: 'checked',
  evidence: [{ id: 'ev-guarded', kind: 'read_only_environment_state', summary: 'checked',
    scope: { asin: 'A1' }, observation_ref }], findings: [], remaining_gaps: [],
  suggested_updates: [], resolves_issue_ids: [],
}) })
const guardedResult = await guarded.audit({
  taskContext: { round: 1 }, contract: { id: 'contract-3' },
  environmentHandle: { env_idx: 1, environment_session: 'env-1', environment_lease: 'lease-1' },
  executorReport: {},
})
assert.equal(guardedResult.audit.id.startsWith('audit-'), true)
assert.equal(guardedResult.audit.contract_id, 'contract-3')
rmSync(dir, { recursive: true, force: true })
console.log('test_mea_v4_auditor_adapter.mjs: all assertions passed')
