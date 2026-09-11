/** Stage-5 recovery coordination: verify identity, inspect, and never replay purchase. */

import { analyzeJournalCalls } from './journal.js'

function copy(value) { return value == null ? value : JSON.parse(JSON.stringify(value)) }

export function assessRecoveryJournal(events) {
  const analyzed = analyzeJournalCalls(events)
  const unknownPurchase = analyzed.calls.find(({ call, result }) =>
    call.tool_name === 'click'
    && String(call.tool_arguments?.value ?? '').toLowerCase() === 'buy now'
    && result === null)
  const unauditedTerminal = events.some(event => event.event_type === 'tool_result'
    && event.raw_result?.done === true)
    && !events.some(event => event.event_type === 'audit_report')
  return {
    diagnostics: analyzed.diagnostics,
    unknown_purchase_response: unknownPurchase ? copy(unknownPurchase.call) : null,
    unaudited_terminal: unauditedTerminal,
  }
}

export async function coordinateRecovery({
  events, inspector, auditor = null, taskContext = null, contract = null,
  environmentHandle, priorAudits = [], stopExecutor = async () => {},
} = {}) {
  const assessment = assessRecoveryJournal(events ?? [])
  await stopExecutor()
  if (!environmentHandle?.environment_session || !environmentHandle?.environment_lease) {
    return { status: 'recovery_required', reason: 'environment identity unavailable', assessment }
  }
  let evidence
  try {
    evidence = await inspector.inspect(environmentHandle)
  } catch (error) {
    return { status: 'recovery_required', reason: `environment identity/inspection failed: ${error.message}`, assessment }
  }
  if (assessment.unknown_purchase_response) {
    if (!evidence.terminal_receipt?.asin) {
      return { status: 'recovery_required', reason: 'purchase response unknown and no terminal receipt',
        replay_purchase: false, evidence, assessment }
    }
  }
  if (evidence.terminal_receipt?.asin || assessment.unaudited_terminal) {
    if (!auditor || !contract || !taskContext) {
      return { status: 'audit_required', replay_purchase: false, evidence, assessment }
    }
    const audit = await auditor.audit({ taskContext, contract, priorAudits,
      executorReport: { summary: 'recovery audit; executor history withheld' },
      environmentHandle })
    return { status: 'recovered_by_audit', replay_purchase: false, evidence,
      audit, environment_done: Boolean(evidence.terminal_receipt?.asin), assessment }
  }
  if (assessment.diagnostics.length > 0) {
    return { status: 'recovery_required', reason: 'journal has unresolved calls',
      replay_purchase: false, evidence, assessment }
  }
  return { status: 'safe_to_continue', replay_purchase: false, evidence, assessment }
}
