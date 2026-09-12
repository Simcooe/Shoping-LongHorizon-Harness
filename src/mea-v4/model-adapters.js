/** Real bounded Manager/Auditor/Shopper adapters for the stage-5 controller. */

import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

function copy(value) { return value == null ? value : JSON.parse(JSON.stringify(value)) }
function required(value, field) {
  if (value === undefined || value === null || String(value).trim() === '') throw new Error(`${field} is required`)
  return String(value).trim()
}
function appendRoleLog(logDir, role, record) {
  if (!logDir) return
  mkdirSync(logDir, { recursive: true })
  writeFileSync(join(logDir, `${role}.jsonl`), `${JSON.stringify(record)}\n`, { flag: 'a' })
}
const MANAGER_ALLOWED_RULE_KEYS = new Set(['tool', 'allow', 'allow_kinds', 'deny_kinds', 'deny_values'])
function normalizeManagerOutput(output) {
  if (!output || typeof output !== 'object' || Array.isArray(output)) return output
  const rules = output.contract?.tool_rules
  if (Array.isArray(rules)) {
    output.contract.tool_rules = rules.flatMap(rule => {
      if (!rule || typeof rule !== 'object' || Array.isArray(rule) || !rule.tool) return []
      const normalized = Object.fromEntries(Object.entries(rule)
        .filter(([key]) => MANAGER_ALLOWED_RULE_KEYS.has(key)))
      if (Array.isArray(normalized.allow)) {
        const values = normalized.allow.map(String)
        delete normalized.allow
        normalized.allow_kinds = normalized.allow_kinds ?? values
      }
      if (Array.isArray(rule.allow_values) && !normalized.allow_kinds) {
        normalized.allow_kinds = rule.allow_values.map(String)
      }
      return [normalized]
    })
  }
  return output
}
function normalizeAuditorOutput(output) {
  if (!output || typeof output !== 'object' || Array.isArray(output)) return output
  output.evidence = Array.isArray(output.evidence) ? output.evidence : []
  output.findings = Array.isArray(output.findings) ? output.findings : []
  output.remaining_gaps = Array.isArray(output.remaining_gaps) ? output.remaining_gaps : []
  output.suggested_updates = Array.isArray(output.suggested_updates) ? output.suggested_updates : []
  output.resolves_issue_ids = Array.isArray(output.resolves_issue_ids) ? output.resolves_issue_ids : []
  for (const finding of output.findings) {
    if (!finding || typeof finding !== 'object') continue
    if (typeof finding.supported !== 'boolean') finding.supported = false
    if (!['completed', 'pending', 'blocked', 'untrusted'].includes(finding.proposed_status)) {
      // Missing/invalid status can never be repaired upward to completed.
      finding.proposed_status = 'pending'
    }
    finding.evidence_refs = Array.isArray(finding.evidence_refs) ? finding.evidence_refs : []
    finding.dependencies = Array.isArray(finding.dependencies) ? finding.dependencies : []
  }
  return output
}
function jsonText(text) {
  const value = String(text ?? '').trim()
  const fenced = value.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i)
  try { return JSON.parse(fenced ? fenced[1] : value) } catch {
    const start = value.indexOf('{'), end = value.lastIndexOf('}')
    if (start >= 0 && end > start) return JSON.parse(value.slice(start, end + 1))
    throw new Error('role output is not valid JSON')
  }
}

const MANAGER_SYSTEM = `You are the Manager in a long-horizon shopping harness. You have no environment tools. Use only the public task, structured Task State, prior Audit Reports and real shopper replies in the input. Output exactly one JSON object accepted by longhorizon Manager v3:
{"decision":"execute|done|blocked|ask","reason":"...","state_updates":[{"audit_id":"...","finding_id":"...","record_id":"..."}],"contract":null|{"id":"unique","goal":"...","acceptance_criteria":["..."],"boundary_constraints":["..."],"dependencies":[],"relevant_state_ids":["req-1"],"relevant_audit_ids":[],"role_tools":["search","click","finish"],"manager_suggested_tools":["search"],"tool_rules":[{"tool":"click","deny_values":["Buy Now"]}],"budget":{"max_tool_calls":1,"timeout_seconds":120}},"question":null|"one focused question"}.
Never invent state updates: select only findings present in prior audits. Use ask when a decision-critical user requirement is missing or ambiguous; do not ask for information already supplied. Use done only after a complete clean terminal-receipt audit supports all active requirements. Contract ids must be new. tool_rules MUST be an array of structured objects with tool plus allow/allow_kinds/deny_kinds/deny_values; never output natural-language strings there. Use boundary_constraints for natural-language restrictions. The finish tool abandons without purchase and NEVER creates a terminal receipt. When the task requires completing the simulated shopping transaction, only a contract-authorized Buy Now action can produce a receipt; after any terminal environment action do not request another Executor. Keep each episode narrow and bounded.`

const AUDITOR_SYSTEM = `You are an independent read-only Auditor. Ignore Executor claims unless independently supported by the supplied environment inspection. Output exactly one JSON object using longhorizon Audit v3 fields, except schema/id/round/contract_id are runtime-owned and MUST NOT be included:
{"status":"complete|incomplete|blocked","integrity":"clean|suspect|violation","verified_summary":"...","evidence":[{"id":"ev-1","kind":"read_only_environment_state|read_only_terminal_receipt","summary":"...","scope":null|{"asin":"..."},"observation_ref":{"event_id":"...","snapshot_id":"...","environment_session":"..."}}],"findings":[{"finding_id":"finding-1","record_id":"req-1","requirement_version":1,"criterion":"...","supported":true,"proposed_status":"completed|pending|blocked|untrusted","evidence_refs":[{"audit_id":"RUNTIME_AUDIT_ID","evidence_id":"ev-1"}],"dependencies":[],"summary":"...","content":null,"scope":null}],"remaining_gaps":["..."],"suggested_updates":[{"finding_id":"finding-1","record_id":"req-1"}],"resolves_issue_ids":[]}.
Use the exact observation_ref supplied by runtime. In every evidence_refs.audit_id use the supplied auditId. Compare terminal receipt fields against explicit requirements and real shopper replies; any conflicting or unknown required option, quantity, price, model, or packaging must remain pending or blocked. A terminal receipt proves only fields actually present. Missing quantity/price/packaging is unknown. A complete audit requires an actual terminal receipt and sufficient public evidence; otherwise use incomplete. Never propose completed for a requirement when an explicit constraint conflicts with the receipt or remains unknown. Do not read or infer reward, gold, hidden goals, or private facts.`

export class JsonChatRole {
  constructor({ role, apiKey = process.env.DEEPSEEK_API_KEY, baseUrl = process.env.DEEPSEEK_BASE_URL ?? 'https://api.deepseek.com',
    model = process.env.DSH_MODEL ?? 'deepseek-v4-flash', system, maxTokens = 8000,
    temperature = 0, logDir = null, fetchImpl = fetch } = {}) {
    this.role = required(role, 'role')
    this.apiKey = required(apiKey, 'apiKey')
    this.baseUrl = baseUrl.replace(/\/$/, '')
    this.model = model
    this.system = system
    this.maxTokens = maxTokens
    this.temperature = temperature
    this.logDir = logDir
    this.fetchImpl = fetchImpl
  }

  async call(input, { signal } = {}) {
    const requestId = `${this.role}-${randomUUID()}`
    const body = { model: this.model, temperature: this.temperature, max_tokens: this.maxTokens,
      response_format: { type: 'json_object' },
      messages: [{ role: 'system', content: this.system }, { role: 'user', content: JSON.stringify(input) }] }
    const started = Date.now()
    if (process.env.MEA_ROLE_PROGRESS !== '0') {
      console.error(`[mea-v4] ${this.role} request started model=${this.model}`)
    }
    const response = await this.fetchImpl(`${this.baseUrl}/chat/completions`, {
      method: 'POST', signal, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.apiKey}` },
      body: JSON.stringify(body),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      const error = new Error(`${this.role} HTTP ${response.status}: ${payload?.error?.message ?? 'request failed'}`)
      error.code = payload?.error?.code ?? `HTTP_${response.status}`
      const record = { request_id: requestId, role: this.role, model: this.model,
        latency_ms: Date.now() - started, usage: payload.usage ?? null,
        input: copy(input), raw_text: null, http_status: response.status,
        status: 'http_failed', parse_error: null, parsed: null }
      appendRoleLog(this.logDir, this.role, record)
      if (process.env.MEA_ROLE_PROGRESS !== '0') {
        console.error(`[mea-v4] ${this.role} response http_failed status=${response.status} latency_ms=${record.latency_ms}`)
      }
      throw error
    }
    const text = payload?.choices?.[0]?.message?.content
    const baseRecord = { request_id: requestId, role: this.role, model: this.model,
      latency_ms: Date.now() - started, usage: payload.usage ?? null, input: copy(input),
      raw_text: text, finish_reason: payload?.choices?.[0]?.finish_reason ?? null }
    let parsed
    try {
      parsed = jsonText(text)
    } catch (error) {
      appendRoleLog(this.logDir, this.role, { ...baseRecord, status: 'parse_failed',
        parse_error: String(error.message ?? error), parsed: null })
      error.rawText = text
      error.code = error.code ?? 'ROLE_JSON_INVALID'
      if (process.env.MEA_ROLE_PROGRESS !== '0') {
        console.error(`[mea-v4] ${this.role} response parse_failed latency_ms=${baseRecord.latency_ms}`)
      }
      throw error
    }
    const record = { ...baseRecord, status: 'ok', parse_error: null, parsed: copy(parsed) }
    appendRoleLog(this.logDir, this.role, record)
    if (process.env.MEA_ROLE_PROGRESS !== '0') {
      console.error(`[mea-v4] ${this.role} response ok latency_ms=${record.latency_ms} tokens=${record.usage?.total_tokens ?? 'null'}`)
    }
    return parsed
  }
}

export class ManagerModelAdapter {
  constructor(options = {}) { this.client = new JsonChatRole({ role: 'manager', system: MANAGER_SYSTEM, ...options }) }
  plan(input, options) { return this.client.call(input, options).then(normalizeManagerOutput) }
}

export class AuditorModelAdapter {
  constructor(options = {}) { this.client = new JsonChatRole({ role: 'auditor', system: AUDITOR_SYSTEM, ...options }) }
  async audit(input, options) {
    const output = normalizeAuditorOutput(await this.client.call(input, options))
    const requirements = new Map((input.task_state?.requirements ?? [])
      .map(record => [record.id, record]))
    // Runtime identity and immutable requirement fields are protected from model drift.
    for (const finding of output.findings ?? []) {
      const requirement = requirements.get(finding.record_id)
      if (requirement) {
        finding.requirement_version = requirement.requirement_version
        finding.content = null
        finding.scope = null
      }
      for (const ref of finding.evidence_refs ?? []) {
        if (ref.audit_id === 'RUNTIME_AUDIT_ID') ref.audit_id = input.auditId
      }
    }
    return output
  }
}

export class HttpShopperAdapter {
  constructor({ baseUrl = process.env.SHOPPER_BASE_URL, fetchImpl = fetch } = {}) {
    this.baseUrl = required(baseUrl, 'shopper.baseUrl').replace(/\/$/, '')
    this.fetchImpl = fetchImpl
  }
  async ask({ question, session, signal }) {
    const response = await this.fetchImpl(`${this.baseUrl}/ask`, { method: 'POST', signal,
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session, question }) })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      const error = new Error(`shopper request failed: ${payload.error ?? response.status}`)
      error.code = payload.error === 'unknown_session' ? 'SHOPPER_SESSION_LOST' : 'SHOPPER_ERROR'
      throw error
    }
    return { reply: String(payload.reply ?? '') }
  }
}

export { MANAGER_SYSTEM, AUDITOR_SYSTEM, jsonText }
