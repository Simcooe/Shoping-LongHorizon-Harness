/** Stage-4 bounded read-only environment inspection and evidence mapping. */

import { randomUUID } from 'node:crypto'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { createObservationRegistry } from './observation.js'
import { validateAuditReport } from './schema.js'

const INSPECTION_VERSION = 'shopping-inspection-v1'
const PUBLIC_FIELDS = new Set([
  'observation_version', 'page_type', 'search_available', 'actions', 'query',
  'normalized_query', 'page', 'total_pages', 'total_results', 'rank_start',
  'rank_end', 'products', 'product', 'selected_options', 'available_options',
  'selected_price', 'subpage', 'content',
])
const RECEIPT_FIELDS = new Set([
  'asin', 'product', 'title', 'options', 'selected_options', 'amount',
  'price', 'quantity', 'pack_quantity', 'currency',
])

function copy(value) { return value == null ? value : JSON.parse(JSON.stringify(value)) }
function required(value, field) {
  if (value === undefined || value === null || String(value).trim() === '') throw new Error(`${field} is required`)
  return String(value).trim()
}
function project(value, allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value
  return Object.fromEntries(Object.entries(value).filter(([key]) => allowed.has(key)).map(([key, item]) => [key, copy(item)]))
}
function publicSnapshot(response) {
  const source = response?.observation_state ?? response?.observation ?? response?.state ?? response
  const snapshot = project(source, PUBLIC_FIELDS)
  const sanitizeProduct = product => project(product, new Set(['asin', 'title', 'brand', 'category', 'price', 'key_attributes']))
  if (snapshot?.product) snapshot.product = sanitizeProduct(snapshot.product)
  if (Array.isArray(snapshot?.products)) snapshot.products = snapshot.products.map(sanitizeProduct)
  return snapshot
}
function receiptSnapshot(response) {
  const source = response?.receipt ?? response?.order ?? response?.purchase
  if (!source || typeof source !== 'object' || Array.isArray(source)
    || !source.asin) return null
  return project(source, RECEIPT_FIELDS)
}

function auditScope(evidence) {
  const product = evidence.public_snapshot?.product
  const receipt = evidence.terminal_receipt
  const asin = receipt?.asin ?? product?.asin
  if (!asin) return null
  const options = receipt?.options ?? receipt?.selected_options
    ?? evidence.public_snapshot?.selected_options
  return options && Object.keys(options).length > 0 ? { asin, ...options } : { asin }
}
function withTimeout(promise, timeoutMs) {
  let timer
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('auditor timeout')), timeoutMs)
    }),
  ]).finally(() => clearTimeout(timer))
}

export class ReadOnlyInspector {
  constructor({ baseUrl, inspect = null, fetchImpl = fetch, logPath = null } = {}) {
    this.baseUrl = baseUrl ?? ''
    this.inspectBackend = inspect
    this.fetchImpl = fetchImpl
    this.logPath = logPath
    this.busy = Promise.resolve()
  }

  inspect(environmentHandle, { scope = 'current', timeoutMs = 10000 } = {}) {
    const operation = this.busy.then(() => this.#inspect(environmentHandle, { scope, timeoutMs }))
    this.busy = operation.catch(() => {})
    return operation
  }

  async #inspect(handle, { scope, timeoutMs }) {
    if (!handle || handle.env_idx === undefined && handle.envIdx === undefined) throw new Error('inspection requires environment session/lease')
    const session = required(handle.environment_session ?? handle.environmentSession, 'environment_session')
    const lease = required(handle.environment_lease ?? handle.environmentLease ?? handle.lease_id ?? handle.leaseId, 'environment_lease')
    const payload = { version: INSPECTION_VERSION, env_idx: Number(handle.env_idx ?? handle.envIdx), environment_session: session, lease_id: lease, scope }
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    let response
    try {
      response = this.inspectBackend
        ? await this.inspectBackend(copy(payload), handle)
        : await this.#request(payload, controller.signal)
    } finally { clearTimeout(timer) }
    if (!response || response.error) throw new Error(response?.error ?? 'inspection failed')
    const snapshot = publicSnapshot(response)
    const receipt = receiptSnapshot(response)
    const snapshotId = `snapshot-${randomUUID()}`
    const eventId = `inspection-${randomUUID()}`
    const evidence = {
      schema: 'longhorizon-inspection-evidence-v1', version: INSPECTION_VERSION,
      event_id: eventId, snapshot_id: snapshotId, environment_session: session,
      environment_lease: lease, scope, read_only: response.read_only !== false,
      source: response.source ?? 'environment.inspect', public_snapshot: snapshot,
      terminal_receipt: receipt, receipt_status: receipt ? 'present' : 'unknown',
      state_digest: response.state_digest ?? null,
    }
    if (!evidence.read_only) evidence.integrity = 'violation'
    if (this.logPath) writeFileSync(this.logPath, `${JSON.stringify(evidence)}\n`, { flag: 'a' })
    return evidence
  }

  async #request(payload, signal) {
    if (!this.baseUrl) throw new Error('inspection backend or baseUrl is required')
    const response = await this.fetchImpl(`${this.baseUrl}/api/shop_agent`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'public_readonly', ...payload }), signal,
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data?.result?.error ?? `inspection HTTP ${response.status}`)
    return data.result ?? data
  }
}

export class AuditorAdapter {
  constructor({ inspector, runDir = null, auditor = null, observationRegistry = null } = {}) {
    if (!inspector || typeof inspector.inspect !== 'function') throw new Error('AuditorAdapter requires a ReadOnlyInspector')
    this.inspector = inspector
    this.runDir = runDir
    this.auditor = auditor
    this.observationRegistry = observationRegistry ?? createObservationRegistry()
  }

  async audit({ taskContext, contract, priorAudits = [], executorReport, environmentHandle, budget = {} }) {
    const auditId = `audit-${randomUUID()}`
    const evidence = await this.inspector.inspect(environmentHandle, { timeoutMs: budget.timeout_ms ?? 10000 })
    const registry = this.observationRegistry
    registry.register({
      event_id: evidence.event_id, snapshot_id: evidence.snapshot_id,
      environment_session: evidence.environment_session, read_only: evidence.read_only,
      source: evidence.source, scope: evidence.scope, raw_ref: evidence.event_id,
    })
    const observationRef = { event_id: evidence.event_id, snapshot_id: evidence.snapshot_id, environment_session: evidence.environment_session }
    const scope = auditScope(evidence)
    let output
    if (this.auditor) output = await withTimeout(Promise.resolve(this.auditor({
      auditId, task: copy(taskContext?.original_task ?? taskContext?.task), task_state: copy(taskContext?.task_state ?? taskContext?.state),
      contract: copy(contract), prior_audits: copy(priorAudits), executor_report: copy(executorReport), evidence: copy(evidence), observation_ref: observationRef,
    })), budget.timeout_ms ?? 10000)
    else output = { status: evidence.terminal_receipt ? 'complete' : 'incomplete', integrity: evidence.read_only ? 'clean' : 'violation', verified_summary: evidence.terminal_receipt ? '独立只读终局收据可用。' : '未发现可核验终局收据。', evidence: [{ id: `ev-${auditId}`, kind: evidence.terminal_receipt ? 'read_only_terminal_receipt' : 'read_only_environment_state', summary: '独立只读环境快照', scope, observation_ref: observationRef }], findings: [], remaining_gaps: evidence.terminal_receipt ? [] : ['terminal receipt unavailable'], suggested_updates: [], resolves_issue_ids: [] }
    const report = validateAuditReport({ ...output, schema: 'longhorizon-audit-v3', id: auditId, round: Number(taskContext?.round ?? 1), contract_id: contract.id })
    for (const item of report.evidence) registry.resolve(item.observation_ref)
    const result = { audit: report, evidence, observation_registry: registry, audit_id: auditId, read_only: evidence.read_only, status: report.status }
    if (this.runDir) { mkdirSync(join(this.runDir, 'audits'), { recursive: true }); writeFileSync(join(this.runDir, 'audits', `${auditId}.json`), `${JSON.stringify(result, null, 2)}\n`) }
    return result
  }
}

export { INSPECTION_VERSION, PUBLIC_FIELDS, RECEIPT_FIELDS }
