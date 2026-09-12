/** Stage-5 MEA controller: Manager -> Executor -> Auditor -> reducer. */

import { randomUUID } from 'node:crypto'
import { createJournal } from './journal.js'
import {
  applyAuditReport,
  applyManagerOutput,
  createTaskState,
  recordShopperReply,
} from './state.js'
import { validateManagerOutput } from './schema.js'

function copy(value) { return value == null ? value : JSON.parse(JSON.stringify(value)) }
function projectManagerState(state) {
  const value = copy(state)
  return {
    schema: value.schema,
    task: value.task,
    requirements: (value.requirements ?? []).filter(record => record.lifecycle === 'active'),
    artifacts: (value.artifacts ?? []).filter(record => record.valid !== false),
    facts: (value.facts ?? []).filter(record => record.valid !== false),
    shopper_replies: value.shopper_replies ?? [],
    integrity_issues: (value.integrity_issues ?? []).filter(issue => issue.status === 'open'),
    round: value.round,
    decision: value.decision,
  }
}
function projectManagerAudits(audits) {
  const values = audits ?? []
  return values.map((audit, index) => index === values.length - 1 ? copy(audit) : {
    schema: audit.schema, id: audit.id, round: audit.round,
    contract_id: audit.contract_id, status: audit.status, integrity: audit.integrity,
    verified_summary: audit.verified_summary, remaining_gaps: audit.remaining_gaps,
    findings: (audit.findings ?? []).map(finding => ({
      finding_id: finding.finding_id, record_id: finding.record_id,
      requirement_version: finding.requirement_version,
      supported: finding.supported, proposed_status: finding.proposed_status,
      summary: finding.summary,
    })),
  })
}
function now() { return Date.now() }
function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)) }
function required(value, field) {
  if (value === undefined || value === null || String(value).trim() === '') throw new Error(`${field} is required`)
  return String(value).trim()
}
function errorCode(error) { return error?.code ?? 'ERROR' }
function withTimeout(factory, timeoutMs, code, message) {
  const controller = new AbortController()
  let timer
  return Promise.race([
    Promise.resolve().then(() => factory(controller.signal)),
    new Promise((_, reject) => {
      timer = setTimeout(() => {
        controller.abort()
        reject(Object.assign(new Error(message), { code }))
      }, timeoutMs)
    }),
  ]).finally(() => clearTimeout(timer))
}
function hasTerminalReceipt(auditResult) {
  return Boolean(auditResult?.evidence?.terminal_receipt?.asin)
    && auditResult.audit?.status === 'complete'
    && auditResult.audit?.integrity === 'clean'
}

export class MeaController {
  constructor({ task, runId, attemptId, environmentHandle, manager, executor, auditor,
    shopper = null, journalPath = null, journal = null, budget = {}, logger = null } = {}) {
    if (!manager || typeof manager.plan !== 'function') throw new Error('manager.plan is required')
    if (!executor || typeof executor.runEpisode !== 'function') throw new Error('executor.runEpisode is required')
    if (!auditor || typeof auditor.audit !== 'function') throw new Error('auditor.audit is required')
    this.taskRequiresClarification = task?.requires_clarification === true
    this.task = required(task?.original_goal ?? task?.query, 'task.original_goal')
    this.taskId = required(task?.id ?? task?.task_id, 'task.id')
    this.runId = required(runId, 'runId')
    this.attemptId = required(attemptId, 'attemptId')
    this.environmentHandle = copy(environmentHandle)
    this.manager = manager
    this.executor = executor
    this.auditor = auditor
    this.shopper = shopper
    this.budget = {
      maxRounds: Number(budget.max_rounds ?? budget.maxRounds ?? 10),
      maxAsk: Number(budget.max_ask ?? budget.maxAsk ?? 3),
      maxManagerCalls: Number(budget.max_manager_calls ?? budget.maxManagerCalls ?? 30),
      timeoutMs: Number(budget.timeout_ms ?? budget.timeoutMs ?? 180000),
      toolCalls: Number(budget.max_tool_calls ?? budget.maxToolCalls ?? 100),
      managerTimeoutMs: Number(budget.manager_timeout_ms ?? budget.managerTimeoutMs ?? 30000),
      shopperTimeoutMs: Number(budget.shopper_timeout_ms ?? budget.shopperTimeoutMs ?? 30000),
      auditorTimeoutMs: Number(budget.auditor_timeout_ms ?? budget.auditorTimeoutMs ?? 30000),
    }
    this.logger = logger
    this.startedAt = now()
    this.managerCalls = 0
    this.managerDecisions = 0
    this.askCount = 0
    this.round = 0
    this.environmentDone = false
    this.harnessOutcome = 'unresolved'
    this.runtimeStatus = 'running'
    this.episodes = []
    this.audits = []
    this.auditResults = []
    this.totalToolCalls = 0
    this.finalReceiptVerified = false
    this.journal = journal ?? (journalPath ? createJournal(journalPath) : null)
    this.state = createTaskState({
      taskId: this.taskId,
      originalGoal: this.task,
      personaText: task?.persona ?? null,
      initialRequirements: (task?.requirements ?? [{ text: this.task, source: 'initial_request' }]),
    })
  }

  #event(eventType, role, source, payload = {}) {
    if (!this.journal) return
    this.journal.append({ task_id: this.taskId, run_id: this.runId, attempt_id: this.attemptId,
      role, source, event_type: eventType, ...copy(payload) })
  }

  #budgetCheck() {
    if (now() - this.startedAt > this.budget.timeoutMs) {
      const error = new Error('controller budget exhausted: timeout')
      error.code = 'CONTROLLER_TIMEOUT'
      throw error
    }
    if (this.managerCalls >= this.budget.maxManagerCalls) {
      const error = new Error('controller budget exhausted: manager calls')
      error.code = 'MANAGER_BUDGET_EXHAUSTED'
      throw error
    }
  }

  #remainingMs(limit = this.budget.timeoutMs) {
    return Math.max(1, Math.min(limit, this.budget.timeoutMs - (now() - this.startedAt)))
  }

  async #callManager(trigger) {
    this.#budgetCheck()
    this.managerDecisions += 1
    const input = {
      task: { id: this.taskId, original_request: this.task,
        requires_clarification: this.taskRequiresClarification,
        clarification_count: this.state.shopper_replies?.length ?? 0 },
      state: projectManagerState(this.state),
      prior_audits: projectManagerAudits(this.audits),
      trigger,
      budget: { manager_calls_used: this.managerCalls, max_manager_calls: this.budget.maxManagerCalls },
    }
    let lastError = null
    for (let attempt = 0; attempt < 2; attempt += 1) {
      this.#budgetCheck()
      this.managerCalls += 1
      const requestId = `manager-${this.managerCalls}`
      this.#event('role_request', 'manager', 'manager', { request_id: requestId,
        model_visible_text: JSON.stringify(input), budget: { retry: attempt } })
      try {
        const output = await withTimeout(
          signal => this.manager.plan(copy(input), { attempt, signal }),
          this.#remainingMs(this.budget.managerTimeoutMs),
          'MANAGER_TIMEOUT', 'manager request timeout')
        const parsed = validateManagerOutput(output)
        this.#event('role_response', 'manager', 'manager', { request_id: requestId, raw_result: copy(parsed) })
        return parsed
      } catch (error) {
        lastError = error
        this.#event('role_response', 'manager', 'manager', { request_id: requestId, error: String(error.message ?? error), budget: { retry: attempt } })
        if (attempt === 0) {
          input.schema_error = String(error.message ?? error)
          input.retry_instruction = 'Return a shorter complete JSON object only. Correct the schema error; do not repeat commentary.'
          await delay(50)
        }
      }
    }
    throw lastError
  }

  async #ask(question) {
    if (!this.shopper || typeof this.shopper.ask !== 'function') throw new Error('ask requested but shopper adapter is unavailable')
    if (this.askCount >= this.budget.maxAsk) throw Object.assign(new Error('ask budget exhausted'), { code: 'ASK_BUDGET_EXHAUSTED' })
    this.askCount += 1
    const requestId = `ask-${this.askCount}`
    const session = this.environmentHandle?.shopper_session ?? this.environmentHandle?.shopperSession
    this.#event('role_request', 'controller', 'controller', { request_id: requestId, manager_question: question })
    const answer = await withTimeout(
      signal => this.shopper.ask({ question, session, attempt_id: this.attemptId, signal }),
      this.#remainingMs(this.budget.shopperTimeoutMs),
      'SHOPPER_TIMEOUT', 'shopper request timeout')
    const reply = required(answer?.reply, 'shopper.reply')
    this.#event('shopper_qa', 'controller', 'shopper', { request_id: requestId, manager_question: question, shopper_reply: reply, actor: 'controller' })
    this.state = recordShopperReply(this.state, {
      id: requestId, question, reply, eventRef: requestId,
    })
    return reply
  }

  async #execute(contract) {
    if (this.environmentDone) {
      throw Object.assign(new Error('environment already terminal; executor is forbidden'), {
        code: 'ENVIRONMENT_ALREADY_DONE',
      })
    }
    this.round += 1
    if (this.round > this.budget.maxRounds) throw Object.assign(new Error('round budget exhausted'), { code: 'ROUND_BUDGET_EXHAUSTED' })
    const availableSchemas = this.environmentHandle?.tool_schemas ?? []
    const allowedToolNames = new Set(contract.role_tools ?? [])
    const context = {
      task_id: this.taskId, run_id: this.runId, attempt_id: this.attemptId,
      round: this.round, round_id: `round-${this.round}`, original_task: this.task,
      task_state: copy(this.state), prior_audits: copy(this.audits),
      tool_schemas: availableSchemas.filter(schema => allowedToolNames.has(schema.name)),
    }
    const remainingToolCalls = this.budget.toolCalls - this.totalToolCalls
    if (remainingToolCalls <= 0) throw Object.assign(new Error('total tool budget exhausted'), {
      code: 'TOOL_BUDGET_EXHAUSTED',
    })
    const contractToolCalls = Number(contract.budget?.max_tool_calls ?? remainingToolCalls)
    const contractTimeoutMs = Number(contract.budget?.timeout_seconds ?? this.budget.timeoutMs / 1000) * 1000
    const episode = await this.executor.runEpisode('executor', context, contract, this.environmentHandle, {
      timeout_ms: this.#remainingMs(contractTimeoutMs),
      max_tool_calls: Math.max(1, Math.min(remainingToolCalls, contractToolCalls)),
    })
    this.episodes.push(copy(episode))
    this.totalToolCalls += Number(episode.report?.tool_calls ?? 0)
    if (episode.runtime_status === 'budget_exhausted' || episode.runtime_status === 'timeout') {
      return { context, episode, stop_after_audit: episode.runtime_status }
    }
    if (episode.runtime_status !== 'completed') {
      throw Object.assign(new Error(`executor ${episode.runtime_status}`), {
        code: 'EXECUTOR_ERROR',
      })
    }
    if (episode.report?.environment_done || episode.report?.output?.environment_done) this.environmentDone = true
    return { context, episode }
  }

  async #audit(context, contract, episode) {
    const result = await withTimeout(
      () => this.auditor.audit({ taskContext: context, contract, priorAudits: this.audits,
        executorReport: episode.report, environmentHandle: this.environmentHandle,
        budget: { timeout_ms: this.#remainingMs(this.budget.auditorTimeoutMs) } }),
      this.#remainingMs(this.budget.auditorTimeoutMs),
      'AUDITOR_TIMEOUT', 'auditor request timeout')
    if (result.audit.evidence?.length > 0
      && !result.observation_registry?.resolve) {
      throw Object.assign(new Error('auditor evidence lacks runtime observation registry'), {
        code: 'AUDIT_PROVENANCE_MISSING',
      })
    }
    this.audits.push(copy(result.audit))
    this.auditResults.push(copy({ audit: result.audit, evidence: result.evidence }))
    if (hasTerminalReceipt(result)) {
      this.environmentDone = true
      this.finalReceiptVerified = true
    }
    this.#event('audit_report', 'auditor', 'auditor', { round_id: context.round_id, episode_id: episode.episode_id, audit_refs: [result.audit.id], raw_result: copy(result.audit) })
    this.state = applyAuditReport(this.state, result.audit, {
      resolveObservation: result.observation_registry?.resolve
        ? ref => result.observation_registry.resolve(ref) : null,
    })
    return result.audit
  }

  async run() {
    try {
      while (this.harnessOutcome === 'unresolved') {
        this.#budgetCheck()
        const managerOutput = await this.#callManager(this.round === 0 ? 'task_start' : 'post_audit')
        if (this.environmentDone && managerOutput.decision === 'done') {
          try {
            this.state = applyManagerOutput(this.state, managerOutput)
          } catch {
            this.harnessOutcome = 'environment_terminated_unresolved'
            break
          }
        } else if (this.environmentDone && managerOutput.decision === 'execute') {
          // Ignore an impossible follow-up contract after a normal environment
          // terminal; the audited task failed, while the runtime completed.
          this.harnessOutcome = 'environment_terminated_unresolved'
          break
        }
        if (!this.environmentDone) {
          this.state = applyManagerOutput(this.state, managerOutput)
        }
        if (managerOutput.decision === 'ask') {
          await this.#ask(managerOutput.question)
          continue
        }
        if (managerOutput.decision === 'blocked') { this.harnessOutcome = 'blocked'; break }
        if (managerOutput.decision === 'done') {
          if (!this.environmentDone || !this.finalReceiptVerified) {
            throw Object.assign(new Error('audited success requires a verified terminal receipt'), {
              code: 'FINAL_RECEIPT_REQUIRED',
            })
          }
          this.harnessOutcome = 'audited_success'
          break
        }
        if (this.environmentDone) {
          this.harnessOutcome = 'environment_terminated_unresolved'
          break
        }
        const executed = await this.#execute(managerOutput.contract)
        await this.#audit(executed.context, managerOutput.contract, executed.episode)
        if (executed.stop_after_audit) {
          this.harnessOutcome = 'budget_exhausted'
          break
        }
      }
      this.runtimeStatus = 'completed'
    } catch (error) {
      this.runtimeStatus = error?.code === 'CONTROLLER_TIMEOUT' ? 'interrupted' : 'failed'
      if (this.harnessOutcome === 'unresolved') this.harnessOutcome =
        error?.code?.includes('BUDGET') || error?.code?.endsWith('_TIMEOUT')
          || error?.code === 'CONTROLLER_TIMEOUT' ? 'budget_exhausted'
          : error?.code === 'ENVIRONMENT_ALREADY_DONE' || error?.code === 'FINAL_RECEIPT_REQUIRED'
            ? 'unresolved' : 'unresolved'
      this.#event('runtime_status', 'controller', 'runtime', { runtime_status: this.runtimeStatus, error: String(error.message ?? error), budget: { code: errorCode(error) } })
    } finally {
      this.journal?.close()
    }
    return {
      schema: 'longhorizon-controller-report-v1', task_id: this.taskId, run_id: this.runId, attempt_id: this.attemptId,
      state: copy(this.state), episodes: copy(this.episodes), audits: copy(this.audits),
      environment_done: this.environmentDone, harness_outcome: this.harnessOutcome,
      runtime_status: this.runtimeStatus, manager_calls: this.managerCalls,
      manager_decisions: this.managerDecisions, ask_count: this.askCount,
      total_tool_calls: this.totalToolCalls, final_receipt_verified: this.finalReceiptVerified,
      task_success: this.harnessOutcome === 'audited_success',
      rounds: this.round, elapsed_ms: now() - this.startedAt,
    }
  }
}

export function createMeaController(options) { return new MeaController(options) }
