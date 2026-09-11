/**
 * Fresh-context DSH AgentAdapter (stage 3).
 *
 * The adapter owns the episode boundary, not the planner.  Every call creates a
 * new DSH_HOME/session and passes only the public task, state, contract, audit
 * references and tool schemas to the executor.  A backend may be injected for
 * tests; the default backend runs the native DSH CLI and never resumes a
 * previous session.
 */

import {
  existsSync, mkdirSync, readFileSync, readdirSync, symlinkSync, writeFileSync,
} from 'node:fs'
import { randomUUID } from 'node:crypto'
import { join } from 'node:path'
import { spawn, execFileSync } from 'node:child_process'
import { createJournal, newEventId } from './journal.js'
import { episodeLogDir } from './ids.js'

const EXECUTOR_TOOLS = new Set(['search', 'click', 'finish', 'ask_shopper'])
const ROLES = new Set(['executor'])

function copy(value) { return value == null ? value : JSON.parse(JSON.stringify(value)) }
function required(value, field) {
  if (value === undefined || value === null || String(value).trim() === '') throw new Error(`${field} is required`)
  return String(value).trim()
}
function positive(value, field, fallback) {
  const n = Number(value ?? fallback)
  if (!Number.isFinite(n) || n <= 0) throw new Error(`${field} must be positive`)
  return n
}
function safe(value, field) {
  const s = required(value, field)
  if (s === '.' || s === '..' || s.includes('/') || s.includes('\\')) throw new Error(`${field} must be a safe path segment`)
  return s
}
function publicInput(taskContext, contract, auditRefs, toolSchemas) {
  const ctx = taskContext ?? {}
  return {
    task: copy(ctx.original_task ?? ctx.task ?? ctx.query ?? ''),
    task_state: copy(ctx.task_state ?? ctx.state ?? null),
    contract: copy(contract ?? null),
    prior_audits: copy(auditRefs ?? ctx.prior_audits ?? []),
    tool_schemas: copy(toolSchemas ?? ctx.tool_schemas ?? []),
    environment: copy(ctx.environment ?? ctx.environment_entry ?? null),
  }
}
function validateTools(schemas, contract = null) {
  if (!Array.isArray(schemas)) return []
  const contractTools = new Set(contract?.role_tools ?? [...EXECUTOR_TOOLS])
  const deniedTools = new Set((contract?.tool_rules ?? [])
    .filter(rule => rule.allow === false).map(rule => rule.tool))
  return schemas.map((schema, index) => {
    if (!schema || typeof schema !== 'object' || !EXECUTOR_TOOLS.has(schema.name)
      || !contractTools.has(schema.name) || deniedTools.has(schema.name)) {
      throw new Error(`tool_schemas[${index}] is not allowed by the executor contract`)
    }
    return copy(schema)
  })
}

function findSessionFile(root) {
  if (!existsSync(root)) return null
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name)
    if (entry.isDirectory()) {
      const found = findSessionFile(path)
      if (found) return found
    } else if (entry.name === 'session.jsonl.zstd') return path
  }
  return null
}
function textOfMessage(message) {
  const blocks = message?.content ?? []
  for (const block of blocks) {
    if (block?.type === 'text') return block.text ?? ''
    if (Array.isArray(block?.content)) {
      const text = block.content.filter(item => item?.type === 'text').map(item => item.text ?? '').join('\n')
      if (text) return text
    }
  }
  return ''
}
function readDshEvents(sessionPath) {
  const raw = execFileSync('zstd', ['-dc', sessionPath], { encoding: 'utf8', maxBuffer: 100 * 1024 * 1024 })
  return raw.split(/\r?\n/).filter(Boolean).flatMap(line => {
    try { return [JSON.parse(line)] } catch { return [] }
  })
}

function dshPrompt(input) {
  return [
    'You are a fresh-context shopping Executor.',
    'Use only the public JSON packet below. Do not assume access to prior chat history, old sessions, audit logs, hidden goals, reward, or private shopper facts.',
    'Execute the supplied contract with the available shopping tools. Do not expose or invent audit conclusions.',
    JSON.stringify(input),
  ].join('\n\n')
}

async function runProcess(command, args, { cwd, env, timeoutMs, onOutput }) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { cwd, env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = '', stderr = '', timedOut = false, settled = false
    const finish = (result) => { if (!settled) { settled = true; resolve(result) } }
    child.stdout.on('data', chunk => { stdout += chunk.toString(); onOutput?.(chunk.toString(), 'stdout') })
    child.stderr.on('data', chunk => { stderr += chunk.toString(); onOutput?.(chunk.toString(), 'stderr') })
    const timer = setTimeout(() => {
      timedOut = true
      try { process.kill(-child.pid, 'SIGTERM') } catch {}
      setTimeout(() => { try { process.kill(-child.pid, 'SIGKILL') } catch {} }, 250)
    }, timeoutMs)
    child.on('error', error => { clearTimeout(timer); finish({ code: null, signal: null, stdout, stderr, error, timedOut }) })
    child.on('close', (code, signal) => { clearTimeout(timer); finish({ code, signal, stdout, stderr, timedOut }) })
  })
}

export class AgentAdapter {
  constructor({ runDir, profile = 'mea-v4-paper', cwd = process.cwd(), backend = null,
    journal = null, command = 'pnpm', logger = null, profilesDir = null } = {}) {
    this.runDir = required(runDir ?? join(process.cwd(), '.mea-v4-run'), 'runDir')
    this.profile = profile
    this.cwd = cwd
    this.backend = backend
    this.command = command
    this.logger = logger
    this.profilesDir = profilesDir ?? process.env.DSH_PROFILES_DIR
      ?? (process.env.DSH_HOME ? join(process.env.DSH_HOME, 'profiles') : join(process.cwd(), '.dsh-home', 'profiles'))
    mkdirSync(this.runDir, { recursive: true })
    this.journal = journal
    this.journals = new Map()
  }

  async runEpisode(role, taskContext, contract, environmentHandle, budget = {}) {
    if (!ROLES.has(role)) throw new Error(`unsupported episode role "${role}"`)
    const episodeId = `episode-${randomUUID()}`
    const roundId = required(taskContext?.round_id ?? taskContext?.roundId ?? 'round-1', 'round_id')
    const taskId = required(taskContext?.task_id ?? taskContext?.taskId, 'task_id')
    const runId = required(taskContext?.run_id ?? taskContext?.runId, 'run_id')
    const attemptId = required(taskContext?.attempt_id ?? taskContext?.attemptId, 'attempt_id')
    const taskJournal = this.journal ?? (() => {
      const key = `${taskId}/${runId}/${attemptId}`
      if (!this.journals.has(key)) {
        this.journals.set(key, createJournal(join(this.runDir, 'journal', `${safe(taskId, 'task_id')}.jsonl`)))
      }
      return this.journals.get(key)
    })()
    const timeoutMs = positive(budget.timeout_ms ?? budget.timeoutMs, 'budget.timeout_ms', 60000)
    const maxToolCalls = Math.max(1, Math.floor(Number(budget.max_tool_calls ?? budget.maxToolCalls ?? 20)))
    const schemas = validateTools(taskContext?.tool_schemas ?? taskContext?.toolSchemas ?? environmentHandle?.tool_schemas ?? [], contract)
    const input = publicInput(taskContext, contract, taskContext?.prior_audits ?? taskContext?.audit_refs, schemas)
    const logDir = episodeLogDir(this.runDir, role, episodeId)
    mkdirSync(logDir, { recursive: true })
    const logPath = join(logDir, 'episode.jsonl')
    const appendLog = event => { writeFileSync(logPath, `${JSON.stringify({ ts: Date.now(), ...event })}\n`, { flag: 'a' }) }
    const state = { toolCalls: 0, timedOut: false, budgetExceeded: false,
      status: 'running', output: null, environmentDone: false, terminalEventId: null }
    appendLog({ event_type: 'episode_start', episode_id: episodeId, round_id: roundId, input: copy(input), budget: { timeout_ms: timeoutMs, max_tool_calls: maxToolCalls } })
    taskJournal.append({ task_id: taskId, run_id: runId, attempt_id: attemptId,
      round_id: roundId, episode_id: episodeId, role: 'executor', source: 'runtime',
      event_type: 'episode_start', original_task: input.task,
      budget: { timeout_ms: timeoutMs, max_tool_calls: maxToolCalls } })
    const event = (name, payload = {}) => {
      if (name === 'tool_call') {
        state.toolCalls += 1
        if (state.toolCalls > maxToolCalls) {
          state.budgetExceeded = true
          const error = new Error('tool budget exhausted')
          error.code = 'TOOL_BUDGET_EXHAUSTED'
          appendLog({ event_type: 'budget_exhausted', episode_id: episodeId, round_id: roundId, tool_calls: state.toolCalls })
          throw error
        }
      }
      appendLog({ event_type: name, episode_id: episodeId, round_id: roundId, ...copy(payload) })
      if (name === 'tool_call') {
        taskJournal.append({ task_id: taskId, run_id: runId, attempt_id: attemptId,
          round_id: roundId, episode_id: episodeId, role: 'executor', source: 'executor',
          event_type: 'tool_call', local_step: payload.local_step ?? 0,
          call_id: required(payload.call_id, 'call_id'),
          tool_call_id: String(payload.tool_call_id ?? payload.call_id),
          tool_name: required(payload.tool_name, 'tool_name'),
          tool_arguments: copy(payload.tool_arguments ?? {}) })
      } else if (name === 'tool_result') {
        const appended = taskJournal.append({ task_id: taskId, run_id: runId, attempt_id: attemptId,
          round_id: roundId, episode_id: episodeId, role: 'executor', source: 'environment',
          event_type: 'tool_result', local_step: payload.local_step ?? 0,
          call_id: required(payload.call_id, 'call_id'), raw_result: copy(payload.raw_result ?? {}),
          model_visible_text: payload.model_visible_text ?? '',
          ...(typeof payload.raw_result?.done === 'boolean'
            ? { raw_done: payload.raw_result.done, env_done: payload.raw_result.done } : {}) })
        if (payload.raw_result?.done === true && !state.environmentDone) {
          state.environmentDone = true
          state.terminalEventId = appended.event_id
        }
      }
      this.logger?.({ event_type: name, episode_id: episodeId, ...copy(payload) })
    }
    let result
    try {
      if (this.backend?.runEpisode) {
        let timer
        result = await Promise.race([
          this.backend.runEpisode({
            episodeId, roundId, role, input: copy(input), environmentHandle: copy(environmentHandle),
            budget: { timeoutMs, maxToolCalls }, event,
          }),
          new Promise(resolve => {
            timer = setTimeout(() => resolve({ timedOut: true, error: 'episode timeout' }), timeoutMs)
          }),
        ]).finally(() => clearTimeout(timer))
      } else {
        result = await this.#runDsh({ episodeId, input, environmentHandle, timeoutMs, maxToolCalls, logDir, event })
      }
      if (state.budgetExceeded) state.status = 'budget_exhausted'
      else if (state.timedOut || result?.timedOut) state.status = 'timeout'
      else if (result?.error || result?.code > 0) state.status = 'error'
      else state.status = 'completed'
    } catch (error) {
      state.status = error?.code === 'TOOL_BUDGET_EXHAUSTED' ? 'budget_exhausted' : 'error'
      result = { error: String(error?.message ?? error), error_code: error?.code ?? 'EPISODE_ERROR' }
    }
    const report = {
      schema: 'longhorizon-executor-report-v1', episode_id: episodeId, round_id: roundId,
      role, status: state.status,
      tool_calls: Math.min(state.toolCalls, maxToolCalls),
      requested_tool_calls: state.toolCalls,
      rejected_tool_calls: state.budgetExceeded ? Math.max(0, state.toolCalls - maxToolCalls) : 0,
      environment_done: state.environmentDone, terminal_event_id: state.terminalEventId,
      summary: typeof result?.summary === 'string' ? result.summary : '',
      output: copy(result?.output ?? result?.final_text ?? null),
      error: result?.error ? String(result.error) : null,
    }
    appendLog({ event_type: 'episode_end', episode_id: episodeId, round_id: roundId, status: state.status, report })
    taskJournal.append({ task_id: taskId, run_id: runId, attempt_id: attemptId,
      round_id: roundId, episode_id: episodeId, role: 'executor', source: 'runtime',
      event_type: 'episode_end', episode_status: state.status,
      timeout: state.status === 'timeout', budget: { tool_calls: state.toolCalls, max_tool_calls: maxToolCalls } })
    return { report, runtime_status: state.status, episode_id: episodeId, log_path: logPath, log_dir: logDir }
  }

  async #runDsh({ episodeId, input, environmentHandle, timeoutMs, maxToolCalls, logDir, event }) {
    const home = join(logDir, 'dsh-home')
    mkdirSync(home, { recursive: true })
    const episodeProfiles = join(home, 'profiles')
    if (!existsSync(episodeProfiles)) {
      if (!existsSync(this.profilesDir)) throw new Error(`DSH profiles directory not found: ${this.profilesDir}`)
      symlinkSync(this.profilesDir, episodeProfiles, 'dir')
    }
    const env = { ...process.env, ...(environmentHandle?.env ?? {}), DSH_HOME: home,
      SHOPSIM_ENV_IDX: String(environmentHandle?.env_idx ?? environmentHandle?.envIdx ?? ''),
      SHOPPER_SESSION_KEY: String(environmentHandle?.shopper_session ?? environmentHandle?.shopperSession ?? ''),
      SHOPPER_SESSION_SCHEME: 'explicit-v1',
      MEA_ALLOWED_TOOLS: (input.contract?.role_tools ?? []).join(','),
      MEA_MAX_TOOL_CALLS: String(maxToolCalls),
      MEA_TOOL_RULES: JSON.stringify(input.contract?.tool_rules ?? []),
    }
    const task = dshPrompt(input)
    const result = await runProcess(this.command, ['dsh', '--profile', this.profile, task], {
      cwd: this.cwd, env, timeoutMs, onOutput: (text, stream) => event('process_output', { stream, text }),
    })
    const finalText = result.stdout.trim().split(/\r?\n/).filter(Boolean).at(-1) ?? ''
    const sessionPath = findSessionFile(join(home, 'sessions'))
    if (sessionPath) {
      const calls = new Map()
      for (const item of readDshEvents(sessionPath)) {
        const data = item.data ?? {}
        if (item.type === 'tool/call') {
          let args = data.arguments
          try { args = typeof args === 'string' ? JSON.parse(args) : args } catch {}
          calls.set(data.callId, data.step)
          event('tool_call', { call_id: data.callId, tool_call_id: data.callId,
            tool_name: data.name, tool_arguments: args ?? {}, local_step: data.step ?? 0 })
        } else if (item.type === 'tool/result') {
          const meta = data.message?.meta ?? {}
          event('tool_result', { call_id: data.message?.source?.callId
              ?? data.message?.content?.[0]?.toolCallId,
            local_step: data.step ?? calls.get(data.message?.source?.callId) ?? 0,
            raw_result: meta.raw ?? {}, model_visible_text: textOfMessage(data.message) })
        }
      }
    }
    if (result.timedOut) return { ...result, timedOut: true, error: 'episode timeout', final_text: finalText }
    if (result.code !== 0) return { ...result, error: result.stderr || `dsh exited ${result.code}`, final_text: finalText }
    return { ...result, final_text: finalText, output: finalText }
  }
}

export function createAgentAdapter(options) { return new AgentAdapter(options) }
export { dshPrompt, validateTools }
