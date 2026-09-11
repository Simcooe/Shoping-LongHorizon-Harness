/** Task-level exporter for mea-v4 journals. */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname } from 'node:path'
import { validateEvent } from './journal.js'

function copy(value) { return value == null ? value : JSON.parse(JSON.stringify(value)) }
function lines(path) {
  return readFileSync(path, 'utf8').split(/\r?\n/).filter(Boolean).map(line => validateEvent(JSON.parse(line)))
}
function terminalFrom(steps) {
  for (const step of steps) if (step.raw?.done === true) return copy(step.raw)
  return steps.at(-1)?.raw ?? { done: false }
}
function taskOf(events, fallback = '') {
  const event = events.find(item => typeof item.task === 'string' || typeof item.original_task === 'string')
  return event?.task ?? event?.original_task ?? fallback
}

/**
 * Merge the stage-1 journal into Judge-compatible paired traces.
 * Manager/Auditor events are deliberately omitted from shopping traces.
 */
export function buildTaskTraces(events, { task = '' } = {}) {
  const valid = events.map(validateEvent).sort((a, b) => a.sequence - b.sequence)
  const calls = new Map()
  const ordered = []
  const asks = []
  for (const event of valid) {
    if (event.event_type === 'shopper_qa') {
      asks.push({
        event_id: event.event_id, step: event.local_step ?? null,
        event_type: 'ask_shopper', actor: event.role,
        question: event.manager_question, reply: event.shopper_reply,
        sequence: event.sequence,
      })
      const synthetic = {
        step: event.sequence, local_step: event.local_step ?? null,
        event_id: event.event_id, result_event_id: event.event_id,
        call_id: null, tool_name: 'ask_shopper',
        tool_args: { question: event.manager_question },
        observation: `用户回复：${event.shopper_reply}`,
        actor: event.role, sequence: event.sequence,
        raw: { question: event.manager_question, reply: event.shopper_reply,
          actor: event.role, controller_event: true },
      }
      ordered.push({ synthetic })
      continue
    }
    if (event.role !== 'executor') continue
    if (event.event_type === 'tool_call') {
      const record = { call: event, result: null }
      calls.set(event.call_id, record)
      ordered.push(record)
    } else if (event.event_type === 'tool_result') {
      const record = calls.get(event.call_id)
      if (!record) throw new Error(`tool result without call: ${event.call_id}`)
      record.result = event
    }
  }
  const modelSteps = []
  const rawSteps = []
  for (const record of ordered.sort((a, b) =>
    (a.synthetic?.sequence ?? a.call.sequence) - (b.synthetic?.sequence ?? b.call.sequence))) {
    if (record.synthetic) {
      const { raw, ...model } = record.synthetic
      modelSteps.push(model)
      rawSteps.push({ ...model, raw })
      continue
    }
    if (!record.result) throw new Error(`missing tool result: ${record.call.call_id}`)
    const call = record.call, result = record.result
    const args = copy(call.tool_arguments ?? null)
    const raw = copy(result.raw_result ?? { raw_missing: true })
    const step = call.local_step ?? result.local_step ?? null
    modelSteps.push({
      step, local_step: step, event_id: call.event_id, result_event_id: result.event_id,
      call_id: call.call_id, tool_name: call.tool_name, tool_args: args,
      observation: result.model_visible_text ?? '',
    })
    rawSteps.push({
      step, local_step: step, event_id: call.event_id, result_event_id: result.event_id,
      call_id: call.call_id, tool_name: call.tool_name, tool_args: args, raw,
    })
  }
  const originalTask = task || taskOf(valid)
  const firstDoneEvent = valid.find(event => event.event_type === 'tool_result'
    && event.raw_result?.done === true)
  const terminal = firstDoneEvent ? copy(firstDoneEvent.raw_result) : terminalFrom(rawSteps)
  return {
    model_trace: {
      task: originalTask, step_count: modelSteps.length, steps: modelSteps,
      shopper_qa: asks, terminal: {
        done: terminal.done ?? false,
        termination_reason: terminal.termination_reason ?? null,
        reward: terminal.reward ?? null,
        reward_valid: terminal.reward_valid ?? null,
        reward_type: terminal.reward_type ?? null,
        purchase_success: terminal.purchase_success ?? null,
        purchase: terminal.purchase ?? null,
      }, terminal_protocol: 'terminal-protocol-v2',
    },
    raw_trace: {
      task: originalTask, step_count: rawSteps.length, steps: rawSteps,
      shopper_qa: asks, terminal_protocol: 'terminal-protocol-v2',
      terminal: copy(terminal),
    },
  }
}

export function exportTaskJournal(journalPath, outDir, options = {}) {
  const traces = buildTaskTraces(lines(journalPath), options)
  mkdirSync(outDir, { recursive: true })
  const id = options.id ?? 'task'
  const modelPath = `${outDir}/${id}.model_trace.json`
  const rawPath = `${outDir}/${id}.raw_trace.json`
  writeFileSync(modelPath, `${JSON.stringify(traces.model_trace, null, 2)}\n`)
  writeFileSync(rawPath, `${JSON.stringify(traces.raw_trace, null, 2)}\n`)
  return { ...traces, model_path: modelPath, raw_path: rawPath }
}
