#!/usr/bin/env node
/** Run one real mea-v4 controller attempt; used by run_benchmark.py. */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { AgentAdapter } from '../src/mea-v4/agent-adapter.js'
import { AuditorAdapter, ReadOnlyInspector } from '../src/mea-v4/auditor-adapter.js'
import { createMeaController } from '../src/mea-v4/controller.js'
import { AuditorModelAdapter, HttpShopperAdapter, ManagerModelAdapter } from '../src/mea-v4/model-adapters.js'
import { exportTaskJournal } from '../src/mea-v4/exporter.js'
import { createJournal } from '../src/mea-v4/journal.js'

const configPath = process.argv[2]
if (!configPath) throw new Error('usage: node scripts/run_mea_v4_controller.mjs <config.json>')
const config = JSON.parse(readFileSync(configPath, 'utf8'))
mkdirSync(config.attempt_dir, { recursive: true })
const rolesDir = join(config.attempt_dir, 'roles')
const journalPath = join(config.attempt_dir, 'journal.jsonl')
const journal = createJournal(journalPath)

const manager = new ManagerModelAdapter({ model: config.models?.manager, logDir: rolesDir })
const auditorModel = new AuditorModelAdapter({ model: config.models?.auditor, logDir: rolesDir })
const executor = new AgentAdapter({
  runDir: config.attempt_dir, profile: config.profile, cwd: config.dsh_checkout,
  profilesDir: config.profiles_dir, journal,
})
const inspector = new ReadOnlyInspector({
  baseUrl: config.shopsim_base_url,
  logPath: join(config.attempt_dir, 'inspection.jsonl'),
})
const auditor = new AuditorAdapter({ inspector, runDir: config.attempt_dir,
  auditor: (input, options) => auditorModel.audit(input, options) })
const shopper = config.shopper_base_url
  ? new HttpShopperAdapter({ baseUrl: config.shopper_base_url }) : null

const controller = createMeaController({
  task: config.task, runId: config.run_id, attemptId: config.attempt_id,
  environmentHandle: config.environment_handle,
  manager, executor, auditor, shopper, journal,
  budget: config.budget,
})
const report = await controller.run()
const reportPath = join(config.attempt_dir, 'controller-report.json')
writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`)
let traces = null
try {
  traces = exportTaskJournal(journalPath, config.traces_dir, {
    id: config.task.id, task: config.task.original_goal,
  })
} catch (error) {
  report.export_error = String(error.message ?? error)
  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`)
}
console.log(JSON.stringify({ report_path: reportPath,
  model_trace: traces?.model_path ?? null, raw_trace: traces?.raw_path ?? null,
  runtime_status: report.runtime_status, harness_outcome: report.harness_outcome }))
process.exitCode = report.runtime_status === 'completed' ? 0 : 1
