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
const SHOP_TOOLS = new Set(['search', 'click', 'finish', 'ask_shopper'])
const RECORD_STATUSES = new Set(['completed', 'pending', 'blocked', 'untrusted'])

function strings(value, max = 20) {
  if (!Array.isArray(value)) return []
  return [...new Set(value.filter(item => typeof item === 'string' && item.trim())
    .map(item => item.trim()))].slice(0, max)
}

function resolveFindingSelector(input, selector) {
  const audits = input?.prior_audits ?? []
  let auditId = null, findingId = null
  if (typeof selector === 'string') {
    const parts = selector.split('/').filter(Boolean)
    if (parts.length === 2) [auditId, findingId] = parts
    else findingId = selector
  } else if (selector && typeof selector === 'object') {
    auditId = selector.audit_id ?? null
    findingId = selector.finding_id ?? null
  }
  if (!findingId) return null
  const matches = audits.flatMap(audit => (audit.findings ?? [])
    .filter(finding => finding.finding_id === findingId
      && (!auditId || audit.id === auditId))
    .map(finding => ({ audit_id: audit.id, finding_id: finding.finding_id,
      record_id: finding.record_id })))
  return matches.length === 1 && matches[0].record_id ? matches[0] : null
}

/** Convert a small semantic Manager response into the runtime-owned v3 protocol. */
function normalizeManagerOutput(output, input) {
  if (!output || typeof output !== 'object' || Array.isArray(output)) return output
  const decision = ['execute', 'done', 'blocked', 'ask'].includes(output.decision)
    ? output.decision : 'blocked'
  const selectors = output.apply_findings ?? output.state_updates ?? []
  const stateUpdates = (Array.isArray(selectors) ? selectors : [])
    .map(selector => resolveFindingSelector(input, selector))
    .filter(Boolean)
  let contract = null
  if (decision === 'execute') {
    const source = output.contract && typeof output.contract === 'object' ? output.contract : {}
    const available = new Set(strings(input?.available_tools ?? [...SHOP_TOOLS]))
    const requested = strings(source.allowed_tools ?? source.role_tools)
      .filter(tool => SHOP_TOOLS.has(tool) && available.has(tool))
    const roleTools = requested.length > 0 ? requested
      : [...available].filter(tool => SHOP_TOOLS.has(tool))
    const suggested = strings(source.suggested_tools ?? source.manager_suggested_tools)
      .filter(tool => roleTools.includes(tool))
    const activeRecords = (input?.state?.requirements ?? [])
      .filter(record => record.lifecycle !== 'revoked').map(record => record.id)
    const auditIds = new Set((input?.prior_audits ?? []).map(audit => audit.id))
    const maxToolCalls = Math.max(1, Math.min(100,
      Math.floor(Number(source.max_tool_calls ?? source.budget?.max_tool_calls ?? 5) || 5)))
    const timeoutSeconds = Math.max(1, Math.min(86400,
      Number(source.timeout_seconds ?? source.budget?.timeout_seconds ?? 300) || 300))
    const denyValues = strings(source.deny_click_values, 50)
    contract = {
      id: `contract-${randomUUID()}`,
      goal: typeof source.goal === 'string' && source.goal.trim()
        ? source.goal.trim() : 'Advance the next unresolved task requirement.',
      acceptance_criteria: strings(source.acceptance_criteria).length > 0
        ? strings(source.acceptance_criteria) : ['Produce independently auditable progress.'],
      boundary_constraints: strings(source.boundary_constraints),
      dependencies: [],
      relevant_state_ids: activeRecords,
      relevant_audit_ids: strings(source.relevant_audit_ids)
        .filter(id => auditIds.has(id)),
      role_tools: roleTools.length > 0 ? roleTools : ['search'],
      manager_suggested_tools: suggested,
      tool_rules: denyValues.length > 0
        ? [{ tool: 'click', deny_values: denyValues }] : [],
      budget: { max_tool_calls: maxToolCalls, timeout_seconds: timeoutSeconds },
    }
  }
  return {
    decision,
    reason: typeof output.reason === 'string' && output.reason.trim()
      ? output.reason.trim() : 'No reason supplied.',
    state_updates: stateUpdates,
    contract,
    question: decision === 'ask'
      ? (typeof output.question === 'string' && output.question.trim()
          ? output.question.trim() : '请补充完成任务所需的关键信息。')
      : null,
  }
}

/** Normalize only semantic Auditor fields; runtime binds identity and evidence. */
function normalizeAuditorOutput(output) {
  if (!output || typeof output !== 'object' || Array.isArray(output)) return output
  return {
    status: ['complete', 'incomplete', 'blocked'].includes(output.status)
      ? output.status : 'incomplete',
    integrity: ['clean', 'suspect', 'violation'].includes(output.integrity)
      ? output.integrity : 'suspect',
    verified_summary: typeof output.verified_summary === 'string'
      && output.verified_summary.trim() ? output.verified_summary.trim() : 'Audit inconclusive.',
    findings: (Array.isArray(output.findings) ? output.findings : [])
      .filter(finding => finding && typeof finding === 'object')
      .map(finding => ({
        record_id: typeof finding.record_id === 'string' ? finding.record_id : null,
        supported: finding.supported === true,
        proposed_status: RECORD_STATUSES.has(finding.proposed_status ?? finding.status)
          ? (finding.proposed_status ?? finding.status) : 'pending',
        summary: typeof finding.summary === 'string' && finding.summary.trim()
          ? finding.summary.trim() : 'No verified update.',
        content: typeof finding.content === 'string' && finding.content.trim()
          ? finding.content.trim() : null,
      })),
    remaining_gaps: strings(output.remaining_gaps),
    resolves_issue_ids: strings(output.resolves_issue_ids),
  }
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

const MANAGER_SYSTEM = `You are the Manager in a long-horizon shopping harness. You have no environment tools. Read the public task, compact task state, prior audits, and real shopper replies. Return one SMALL JSON object only:
{"decision":"execute|done|blocked|ask","reason":"brief reason","apply_findings":["finding-id or audit-id/finding-id"],"contract":null|{"goal":"one immediate goal","acceptance_criteria":["checkable criterion"],"boundary_constraints":["constraint"],"allowed_tools":["search","click","finish","ask_shopper"],"suggested_tools":["search"],"max_tool_calls":5,"timeout_seconds":300,"deny_click_values":["Buy Now"]},"question":null|"one focused question"}.
Do not emit IDs, versions, evidence references, dependencies, tool-rule objects, state records, or audit schemas; runtime owns them. Select apply_findings only from prior audits. Use ask only when progress requires user information or authorization. Use done only when the audited state satisfies the original task. Use blocked when no permitted subtask can advance it. The finish tool abandons without purchase.`

const AUDITOR_SYSTEM = `You are an independent read-only Auditor. Ignore Executor claims unless the supplied inspection supports them. Return one SMALL JSON object only:
{"status":"complete|incomplete|blocked","integrity":"clean|suspect|violation","verified_summary":"brief summary","findings":[{"record_id":"req-1","supported":true,"status":"completed|pending|blocked|untrusted","summary":"what the inspection proves"}],"remaining_gaps":["missing fact"]}.
Do not emit audit IDs, finding IDs, versions, evidence blocks, evidence references, scopes, dependencies, or suggested updates; runtime binds those deterministically. Evaluate the contract acceptance criteria and boundary constraints against the read-only inspection. Missing or conflicting required information remains pending. Do not read or infer reward, gold, hidden goals, or private facts.`

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
  plan(input, options) { return this.client.call(input, options)
    .then(output => normalizeManagerOutput(output, input)) }
}

export class AuditorModelAdapter {
  constructor(options = {}) { this.client = new JsonChatRole({ role: 'auditor', system: AUDITOR_SYSTEM, ...options }) }
  audit(input, options) { return this.client.call(input, options)
    .then(normalizeAuditorOutput) }
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
