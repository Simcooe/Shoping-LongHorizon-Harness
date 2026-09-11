/** mea-v4 append-only task event journal (stage 1). */

import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { dirname } from 'node:path'

export const JOURNAL_SCHEMA = 'longhorizon-task-journal-v2'

const ROLES = new Set(['controller', 'manager', 'executor', 'auditor'])
const EVENT_TYPES = new Set([
  'episode_start', 'episode_end', 'role_request', 'role_response',
  'tool_call', 'tool_result', 'shopper_qa', 'audit_report', 'runtime_status',
])
const SOURCES = new Set([
  'controller', 'manager', 'executor', 'auditor', 'environment', 'shopper', 'runtime',
])

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function nonEmpty(value, field) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`${field} must be a non-empty string`)
  }
  return value
}

export function newEventId(prefix = 'evt') {
  return `${prefix}-${randomUUID()}`
}

export function validateEvent(event) {
  if (!isObject(event)) throw new Error('event must be an object')
  const allowed = new Set([
    'schema', 'event_id', 'sequence', 'ts', 'task_id', 'run_id', 'attempt_id',
    'round_id', 'episode_id', 'local_step', 'role', 'event_type', 'call_id',
    'tool_call_id', 'request_id', 'tool_name', 'tool_arguments',
    'tool_result', 'model_visible_text', 'raw_result', 'source',
    'manager_question', 'shopper_reply', 'raw_done', 'env_done',
    'runtime_status', 'error', 'budget', 'timeout', 'audit_refs', 'model_usage',
    'task', 'original_task', 'episode_status', 'actor',
  ])
  for (const key of Object.keys(event)) {
    if (!allowed.has(key)) throw new Error(`unknown event field "${key}"`)
  }
  if (event.schema !== undefined && event.schema !== JOURNAL_SCHEMA) {
    throw new Error(`unsupported journal schema "${event.schema}"`)
  }
  nonEmpty(event.event_id, 'event.event_id')
  if (!Number.isInteger(event.sequence) || event.sequence < 1) {
    throw new Error('event.sequence must be a positive integer')
  }
  if (!Number.isFinite(event.ts)) throw new Error('event.ts must be a finite timestamp')
  nonEmpty(event.task_id, 'event.task_id')
  nonEmpty(event.run_id, 'event.run_id')
  nonEmpty(event.attempt_id, 'event.attempt_id')
  if (!ROLES.has(event.role)) throw new Error(`invalid event.role "${event.role}"`)
  if (!EVENT_TYPES.has(event.event_type)) {
    throw new Error(`invalid event.event_type "${event.event_type}"`)
  }
  if (!SOURCES.has(event.source)) throw new Error(`invalid event.source "${event.source}"`)
  if (event.role === 'executor' || event.role === 'auditor') {
    nonEmpty(event.round_id, 'event.round_id')
    nonEmpty(event.episode_id, 'event.episode_id')
  }
  if (event.local_step !== undefined
    && (!Number.isInteger(event.local_step) || event.local_step < 0)) {
    throw new Error('event.local_step must be a non-negative integer')
  }
  if (event.event_type === 'episode_start' || event.event_type === 'episode_end') {
    nonEmpty(event.episode_id, 'event.episode_id')
  }
  if (event.event_type === 'tool_call') {
    if (event.role !== 'executor') throw new Error('tool_call role must be executor')
    nonEmpty(event.call_id, 'event.call_id')
    nonEmpty(event.tool_call_id, 'event.tool_call_id')
    nonEmpty(event.tool_name, 'event.tool_name')
    if (event.source !== 'executor') throw new Error('tool_call source must be executor')
  }
  if (event.event_type === 'tool_result') {
    if (event.role !== 'executor') throw new Error('tool_result role must be executor')
    nonEmpty(event.call_id, 'event.call_id')
    if (event.source !== 'environment') throw new Error('tool_result source must be environment')
    if (!isObject(event.raw_result)) throw new Error('tool_result.raw_result must be an object')
  }
  if (event.event_type === 'shopper_qa') {
    if (event.role !== 'manager' && event.role !== 'controller') {
      throw new Error('shopper_qa role must be manager/controller')
    }
    if (event.source !== 'shopper') throw new Error('shopper_qa source must be shopper')
    nonEmpty(event.manager_question, 'event.manager_question')
    nonEmpty(event.shopper_reply, 'event.shopper_reply')
    if (event.tool_name !== undefined || event.tool_call_id !== undefined
      || event.call_id !== undefined) {
      throw new Error('shopper_qa must not carry executor tool fields')
    }
  }
  if (event.event_type === 'audit_report'
    && (event.role !== 'auditor' || event.source !== 'auditor')) {
    throw new Error('audit_report must originate from auditor')
  }
  if (event.event_type === 'runtime_status'
    && (event.role !== 'controller' || event.source !== 'runtime')) {
    throw new Error('runtime_status must originate from controller/runtime')
  }
  if (event.audit_refs !== undefined && !Array.isArray(event.audit_refs)) {
    throw new Error('event.audit_refs must be an array')
  }
  if (event.model_usage !== undefined && event.model_usage !== null
    && !isObject(event.model_usage)) {
    throw new Error('event.model_usage must be an object or null')
  }
  for (const field of ['raw_done', 'env_done']) {
    if (event[field] !== undefined && typeof event[field] !== 'boolean') {
      throw new Error(`event.${field} must be boolean`)
    }
    if (event[field] !== undefined) {
      if (event.event_type !== 'tool_result' || event.role !== 'executor'
        || event.source !== 'environment') {
        throw new Error(`${field} is only valid on executor environment tool_result`)
      }
      if (event.raw_result.done !== event[field]) {
        throw new Error(`${field} must match raw_result.done`)
      }
    }
  }
  return { ...event, schema: JOURNAL_SCHEMA }
}

function parseExisting(filePath) {
  if (!existsSync(filePath)) return []
  const text = readFileSync(filePath, 'utf8')
  if (!text.trim()) return []
  return text.split('\n').filter(Boolean).map((line, index) => {
    try {
      return validateEvent(JSON.parse(line))
    } catch (error) {
      throw new Error(`invalid existing journal line ${index + 1}: ${error.message}`)
    }
  })
}

export function createJournal(filePath) {
  mkdirSync(dirname(filePath), { recursive: true })
  const existing = parseExisting(filePath)
  const eventIds = new Set()
  let lastSequence = 0
  let identity = null
  for (const event of existing) {
    if (eventIds.has(event.event_id)) throw new Error(`duplicate event_id "${event.event_id}"`)
    if (event.sequence !== lastSequence + 1) {
      throw new Error(`non-contiguous existing sequence at ${event.sequence}`)
    }
    eventIds.add(event.event_id)
    lastSequence = event.sequence
    const current = [event.task_id, event.run_id, event.attempt_id].join('/')
    if (identity === null) identity = current
    else if (identity !== current) throw new Error('journal mixes task/run/attempt identities')
  }
  let closed = false
  return {
    append(input) {
      if (closed) throw new Error('journal is closed')
      const expectedSequence = lastSequence + 1
      const candidate = {
        ...input,
        event_id: input.event_id ?? newEventId(),
        sequence: input.sequence ?? expectedSequence,
        ts: input.ts ?? Date.now(),
      }
      const event = validateEvent(candidate)
      if (event.sequence !== expectedSequence) {
        throw new Error(`event.sequence must be ${expectedSequence}, got ${event.sequence}`)
      }
      if (eventIds.has(event.event_id)) throw new Error(`duplicate event_id "${event.event_id}"`)
      const current = [event.task_id, event.run_id, event.attempt_id].join('/')
      if (identity === null) identity = current
      else if (identity !== current) throw new Error('journal mixes task/run/attempt identities')
      appendFileSync(filePath, `${JSON.stringify(event)}\n`)
      eventIds.add(event.event_id)
      lastSequence = event.sequence
      return event
    },
    close() { closed = true },
    get lastSequence() { return lastSequence },
  }
}

export function appendToolExchange(journal, exchange) {
  const call = journal.append({ ...exchange.call, event_type: 'tool_call' })
  const result = exchange.result
    ? journal.append({ ...exchange.result, event_type: 'tool_result' })
    : null
  return { call, result }
}

export function analyzeJournalCalls(events) {
  const diagnostics = []
  const eventIds = new Set()
  const sequences = new Set()
  const calls = new Map()
  const results = new Set()
  let previousSequence = 0
  for (let index = 0; index < events.length; index += 1) {
    let event
    try {
      event = validateEvent(events[index])
    } catch (error) {
      diagnostics.push({ kind: 'invalid_event', index, error: error.message })
      continue
    }
    if (eventIds.has(event.event_id)) {
      diagnostics.push({ kind: 'duplicate_event_id', event_id: event.event_id })
    }
    if (sequences.has(event.sequence)) {
      diagnostics.push({ kind: 'duplicate_sequence', sequence: event.sequence })
    }
    if (event.sequence !== previousSequence + 1) {
      diagnostics.push({ kind: 'non_contiguous_sequence', sequence: event.sequence })
    }
    eventIds.add(event.event_id)
    sequences.add(event.sequence)
    previousSequence = event.sequence
    if (event.event_type === 'tool_call') {
      if (calls.has(event.call_id)) diagnostics.push({ kind: 'duplicate_call', call_id: event.call_id })
      else calls.set(event.call_id, { call: event, result: null })
    } else if (event.event_type === 'tool_result') {
      if (results.has(event.call_id)) {
        diagnostics.push({ kind: 'duplicate_result', call_id: event.call_id })
      } else if (!calls.has(event.call_id)) {
        diagnostics.push({ kind: 'result_without_call', call_id: event.call_id })
      } else {
        const owner = calls.get(event.call_id).call
        if (owner.task_id !== event.task_id || owner.run_id !== event.run_id
          || owner.attempt_id !== event.attempt_id || owner.round_id !== event.round_id
          || owner.episode_id !== event.episode_id) {
          diagnostics.push({ kind: 'call_identity_mismatch', call_id: event.call_id })
        } else {
          calls.get(event.call_id).result = event
        }
      }
      results.add(event.call_id)
    }
  }
  for (const [callId, record] of calls) {
    if (record.result === null) diagnostics.push({ kind: 'missing_result', call_id: callId })
  }
  return { calls: [...calls.values()], diagnostics }
}
