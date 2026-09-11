#!/usr/bin/env node
/** Real stage-5 smoke runner. Owns reset, explicit shopper session, controller and release. */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { AgentAdapter } from '../src/mea-v4/agent-adapter.js'
import { AuditorAdapter, ReadOnlyInspector } from '../src/mea-v4/auditor-adapter.js'
import { createMeaController } from '../src/mea-v4/controller.js'
import { AuditorModelAdapter, HttpShopperAdapter, ManagerModelAdapter } from '../src/mea-v4/model-adapters.js'
import { createJournal } from '../src/mea-v4/journal.js'
import { exportTaskJournal } from '../src/mea-v4/exporter.js'

const taskId = String(process.argv[2] ?? '')
const originalGoal = process.argv.slice(3).join(' ') || '搜索一个合适商品；开发smoke中不要购买。'
if (!taskId) throw new Error('usage: node scripts/run_mea_v4_smoke.mjs <task-id> [public task]')
const base = (process.env.SHOPSIM_BASE_URL ?? 'http://127.0.0.1:5700').replace(/\/$/, '')
const shopperBase = process.env.SHOPPER_BASE_URL?.replace(/\/$/, '')
const post = async body => {
  const response = await fetch(`${base}/api/shop_agent`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const payload = await response.json()
  if (!response.ok || payload.result?.error) throw new Error(payload.result?.error ?? `HTTP ${response.status}`)
  return payload.result
}
const reset = await post({ action: 'reset', idx: Number(taskId) })
const runId = `mea-v4-smoke-${Date.now()}-${randomUUID().slice(0, 8)}`
const attemptId = randomUUID().replaceAll('-', '')
const shopperSession = `${runId}/${taskId}#attempt-${attemptId}`
if (shopperBase) {
  const response = await fetch(`${shopperBase}/start`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session: shopperSession, idx: Number(taskId) }) })
  if (!response.ok) throw new Error(`shopper start HTTP ${response.status}`)
}
const runDir = join(process.cwd(), 'runs', runId)
mkdirSync(runDir, { recursive: true })
const journal = createJournal(join(runDir, 'journal.jsonl'))
const manager = new ManagerModelAdapter({ logDir: join(runDir, 'roles') })
const auditorModel = new AuditorModelAdapter({ logDir: join(runDir, 'roles') })
const executor = new AgentAdapter({ runDir, journal, cwd: join(process.cwd(), 'deepseek-harness'), profilesDir: join(process.cwd(), '.dsh-home', 'profiles') })
const auditor = new AuditorAdapter({
  inspector: new ReadOnlyInspector({ baseUrl: base, logPath: join(runDir, 'inspection.jsonl') }),
  runDir, auditor: input => auditorModel.audit(input),
})
const shopper = shopperBase ? new HttpShopperAdapter({ baseUrl: shopperBase }) : null
let report
try {
  report = await createMeaController({
    task: { id: taskId, original_goal: originalGoal }, runId, attemptId,
    environmentHandle: { env_idx: reset.env_idx, environment_session: reset.environment_session,
      environment_lease: reset.lease_id, shopper_session: shopperSession,
      tool_schemas: [
        { name: 'search', parameters: { type: 'object', properties: { keywords: { type: 'string' } }, required: ['keywords'] } },
        { name: 'click', parameters: { type: 'object', properties: { value: { type: 'string' } }, required: ['value'] } },
        { name: 'finish', parameters: { type: 'object', properties: { reason: { type: 'string' } }, required: ['reason'] } },
      ], env: { SHOPSIM_BASE_URL: base, SHOPPER_BASE_URL: shopperBase ?? '' } },
    manager, executor, auditor, shopper, journal,
    budget: { max_rounds: Number(process.env.MEA_MAX_ROUNDS ?? 3), max_ask: 2,
      max_manager_calls: 8, max_tool_calls: Number(process.env.SHOP_MAX_STEPS ?? 10),
      timeout_ms: Number(process.env.MEA_TASK_TIMEOUT_MS ?? 600000),
      manager_timeout_ms: 120000, auditor_timeout_ms: 120000, shopper_timeout_ms: 60000 },
  }).run()
  writeFileSync(join(runDir, 'controller-report.json'), `${JSON.stringify(report, null, 2)}\n`)
  try { exportTaskJournal(join(runDir, 'journal.jsonl'), join(runDir, 'traces'), { id: taskId, task: originalGoal }) } catch {}
} finally {
  await post({ action: 'release_one', env_idx: reset.env_idx, lease_id: reset.lease_id }).catch(() => {})
}
console.log(JSON.stringify({ run_dir: runDir, report }, null, 2))
