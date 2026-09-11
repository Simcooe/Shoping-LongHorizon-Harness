/** mea-v4 runtime status and resume preconditions (stage 1). */

export function classifyRunStatus(input = {}) {
  const {
    envDone = false,
    harnessDone = false,
    managerDecision = null,
    error = null,
    exportOk = true,
    sessionOk = true,
    recoveryRequired = false,
  } = input
  const base = {
    environment_done: Boolean(envDone),
    harness_outcome: managerDecision,
  }
  if (recoveryRequired) {
    return { ...base, kind: 'recovery_required', runtime_status: 'failed' }
  }
  if (error) {
    return { ...base, kind: 'role_error', runtime_status: 'failed', error }
  }
  if (!exportOk) return { ...base, kind: 'export_failed', runtime_status: 'failed' }
  if (!sessionOk) return { ...base, kind: 'empty_session', runtime_status: 'failed' }
  if (envDone && harnessDone) {
    return { ...base, kind: 'done', runtime_status: 'completed' }
  }
  if (managerDecision === 'blocked') {
    return { ...base, kind: 'blocked', runtime_status: 'completed' }
  }
  if (managerDecision === 'max_rounds') {
    return { ...base, kind: 'max_rounds', runtime_status: 'completed' }
  }
  return { ...base, kind: 'running', runtime_status: 'running' }
}

export function canResume({
  journalOk = false,
  fingerprintMatch = false,
  attemptComplete = false,
  environmentIdentityVerified = false,
  unauditedChanges = true,
}) {
  return attemptComplete && fingerprintMatch && journalOk
    && environmentIdentityVerified && !unauditedChanges
}

export function checkRecoveryPreconditions(input = {}) {
  const journalDiagnostics = Array.isArray(input) ? input : (input.journalDiagnostics ?? [])
  const environmentIdentity = Array.isArray(input)
    ? 'unknown' : (input.environmentIdentity ?? 'unknown')
  const shopperIdentity = Array.isArray(input)
    ? 'unknown' : (input.shopperIdentity ?? 'unknown')
  if (!['verified', 'unknown', 'mismatch'].includes(environmentIdentity)) {
    throw new Error('invalid environmentIdentity')
  }
  if (!['verified', 'unknown', 'mismatch'].includes(shopperIdentity)) {
    throw new Error('invalid shopperIdentity')
  }
  const incompleteCalls = journalDiagnostics.some(d => d.kind === 'missing_result')
  const hasUnresolved = journalDiagnostics.some(d =>
    d.kind === 'duplicate_call' || d.kind === 'duplicate_result'
    || d.kind === 'result_without_call' || d.kind === 'call_identity_mismatch'
    || d.kind === 'out_of_order'
    || d.kind === 'duplicate_event_id' || d.kind === 'duplicate_sequence'
    || d.kind === 'non_contiguous_sequence' || d.kind === 'invalid_event')
  const unauditedChanges = journalDiagnostics.some(d =>
    d.kind === 'unaudited_environment_change'
    || d.kind === 'unknown_purchase_response')
  const identityUnverified = environmentIdentity !== 'verified'
    || shopperIdentity !== 'verified'
    || journalDiagnostics.some(d =>
      d.kind === 'lease_mismatch' || d.kind === 'environment_identity_unknown'
      || d.kind === 'shopper_identity_unknown' || d.kind === 'shopper_session_lost')
  return {
    incomplete_calls: incompleteCalls,
    has_unresolved_calls: hasUnresolved,
    unaudited_changes: unauditedChanges,
    environment_identity_verified: !identityUnverified,
    recovery_required: incompleteCalls || hasUnresolved
      || unauditedChanges || identityUnverified,
  }
}
