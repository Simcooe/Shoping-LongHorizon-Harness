import assert from 'node:assert/strict'
import { coordinateRecovery } from '../src/mea-v4/recovery.js'

const base = { schema: 'longhorizon-task-journal-v2', task_id: 't', run_id: 'r', attempt_id: 'a', round_id: 'round-1', episode_id: 'episode-1' }
const events = [{ ...base, event_id: 'buy-call', sequence: 1, ts: 1, role: 'executor', source: 'executor', event_type: 'tool_call', call_id: 'c1', tool_call_id: 'c1', tool_name: 'click', tool_arguments: { value: 'Buy Now' } }]
let stopped = false
const unresolved = await coordinateRecovery({ events,
  stopExecutor: async () => { stopped = true },
  inspector: { inspect: async () => ({ terminal_receipt: null }) },
  environmentHandle: { environment_session: 'env', environment_lease: 'lease' },
})
assert.equal(stopped, true)
assert.equal(unresolved.status, 'recovery_required')
assert.equal(unresolved.replay_purchase, false)

const recovered = await coordinateRecovery({ events,
  inspector: { inspect: async () => ({ terminal_receipt: { asin: 'A1' } }) },
  auditor: { audit: async () => ({ audit: { id: 'audit-recovery' } }) },
  taskContext: { round: 1 }, contract: { id: 'c' },
  environmentHandle: { environment_session: 'env', environment_lease: 'lease' },
})
assert.equal(recovered.status, 'recovered_by_audit')
assert.equal(recovered.replay_purchase, false)
assert.equal(recovered.environment_done, true)

const lost = await coordinateRecovery({ events, inspector: { inspect: async () => ({}) }, environmentHandle: {} })
assert.equal(lost.status, 'recovery_required')
console.log('test_mea_v4_recovery.mjs: all assertions passed')
