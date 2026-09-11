// mea-v4 stage 1: identity, journal and recovery invariants.
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  assessCompletedAttempt,
  attemptDir,
  createAttemptManifest,
  readAttemptManifest,
  updateAttemptManifest,
  writeAttemptManifest,
  writeCheckpoint,
} from '../src/mea-v4/attempt.js'
import {
  attemptSessionKey,
  episodeLogDir,
  executionIdentity,
  inputFingerprint,
  journalPath,
  legacyAskSessionKey,
  resolveShopperSessionKey,
  runnerPreheatSessionKey,
} from '../src/mea-v4/ids.js'
import {
  analyzeJournalCalls,
  createJournal,
  validateEvent,
} from '../src/mea-v4/journal.js'
import {
  canResume,
  checkRecoveryPreconditions,
  classifyRunStatus,
} from '../src/mea-v4/run-status.js'

const base = {
  task_id: 'task-1',
  run_id: 'run-1',
  attempt_id: 'attempt-1',
}
const call = (id, tool = 'search') => ({
  ...base,
  role: 'executor',
  source: 'executor',
  event_type: 'tool_call',
  round_id: 'round-1',
  episode_id: 'episode-1',
  call_id: id,
  tool_call_id: `tool-${id}`,
  tool_name: tool,
  tool_arguments: {},
})
const result = (id, done = false) => ({
  ...base,
  role: 'executor',
  source: 'environment',
  event_type: 'tool_result',
  round_id: 'round-1',
  episode_id: 'episode-1',
  call_id: id,
  raw_result: { done },
  raw_done: done,
  env_done: done,
  model_visible_text: done ? 'Episode finished.' : 'page',
})

// Explicit new-profile key wins; legacy behavior remains documented and task 0 works.
{
  assert.deepEqual(
    resolveShopperSessionKey({}, { SHOPPER_SESSION_KEY: 'r/0#attempt-1' }),
    { key: 'r/0#attempt-1', scheme: 'explicit-v1' },
  )
  assert.equal(resolveShopperSessionKey({}, { SHOPSIM_TASK_IDX: '42' }).key, '42')
  assert.equal(resolveShopperSessionKey({}, { SHOPSIM_ENV_IDX: '3' }).key, 'env-3')
  assert.equal(runnerPreheatSessionKey('r', 0), 'r/0')
  assert.equal(legacyAskSessionKey(0, 3), '0')
  assert.equal(attemptSessionKey('r', 0, 1), 'r/0#attempt-1')
}

// Logical fingerprint is stable and separate from execution/lease identity.
{
  const input = {
    taskId: '1',
    benchmarkId: 'b',
    query: 'q',
    profile: 'mea-v4-paper',
    profileConfigHash: 'profile-hash',
    stateSchema: 'state-v3',
    journalSchema: 'journal-v2',
    environmentVersion: 'env-v1',
    modelConfigs: { executor: 'm', auditor: 'm', manager: 'm' },
    budgetConfig: { rounds: 10 },
    shopperSessionScheme: 'explicit-v1',
  }
  assert.equal(inputFingerprint(input), inputFingerprint({
    ...input,
    modelConfigs: { manager: 'm', auditor: 'm', executor: 'm' },
  }))
  assert.notEqual(inputFingerprint(input), inputFingerprint({
    ...input,
    environmentVersion: 'env-v2',
  }))
  assert.deepEqual(executionIdentity({
    taskId: '1',
    runId: 'r',
    attemptId: 'a',
    environmentSession: 'env-session',
    shopperSession: 'shopper-session',
  }), {
    task_id: '1',
    run_id: 'r',
    attempt_id: 'a',
    round_id: null,
    episode_id: null,
    environment_session: 'env-session',
    shopper_session: 'shopper-session',
  })
}

// Paths reject traversal.
{
  assert.equal(episodeLogDir('/run', 'executor', 'ep-1'), '/run/executor/ep-1')
  assert.equal(journalPath('/run', 'task-1'), '/run/journal/task-1.jsonl')
  assert.throws(() => journalPath('/run', '../escape'), /safe path segment/)
}

// Attempt manifest/checkpoint is durable and incomplete artifacts never become resume hits.
{
  const dir = mkdtempSync(join(tmpdir(), 'mea-v4-attempt-'))
  const root = attemptDir(dir, 'task-1', 'attempt-1')
  const manifestPath = join(root, 'manifest.json')
  const checkpointPath = join(root, 'checkpoint.json')
  const manifest = createAttemptManifest({
    taskId: 'task-1',
    runId: 'run-1',
    attemptId: 'attempt-1',
    inputFingerprint: 'fingerprint-1',
    environmentSession: 'env-session-1',
    environmentLease: 'lease-1',
    shopperSession: 'shopper-session-1',
  })
  writeAttemptManifest(manifestPath, manifest)
  assert.equal(readAttemptManifest(manifestPath).runtime_status, 'initialized')
  assert.equal(assessCompletedAttempt(manifest, { fingerprint: 'fingerprint-1' }).can_skip, false)
  writeCheckpoint(checkpointPath, {
    sequence: 3,
    stateHash: 'state-hash',
    unauditedChanges: false,
    environmentDone: true,
  })
  const complete = updateAttemptManifest(manifestPath, {
    runtime_status: 'completed',
    protocol_valid: true,
    export_ok: true,
    session_ok: true,
    attempt_complete: true,
    unaudited_changes: false,
  })
  assert.equal(assessCompletedAttempt(complete, { fingerprint: 'fingerprint-1' }).can_skip, true)
  assert.deepEqual(
    assessCompletedAttempt(complete, { fingerprint: 'other' }).reasons,
    ['fingerprint_mismatch'],
  )
  rmSync(dir, { recursive: true, force: true })
}

// Journal assigns contiguous sequence and supports results returning in call order B then A.
{
  const dir = mkdtempSync(join(tmpdir(), 'mea-v4-journal-'))
  const file = join(dir, 'task.jsonl')
  const journal = createJournal(file)
  const callA = journal.append(call('A'))
  const callB = journal.append(call('B', 'click'))
  const resultB = journal.append(result('B'))
  const resultA = journal.append(result('A'))
  assert.deepEqual(
    [callA.sequence, callB.sequence, resultB.sequence, resultA.sequence],
    [1, 2, 3, 4],
  )
  const events = readFileSync(file, 'utf8').trim().split('\n').map(JSON.parse)
  assert.deepEqual(analyzeJournalCalls(events).diagnostics, [])

  // Reopening resumes at the persisted sequence.
  journal.close()
  const reopened = createJournal(file)
  assert.equal(reopened.append({
    ...base,
    role: 'controller',
    source: 'runtime',
    event_type: 'runtime_status',
    runtime_status: 'running',
  }).sequence, 5)

  // Duplicate identity and explicit sequence regression are rejected before append.
  const duplicateId = events[0].event_id
  assert.throws(() => reopened.append({
    ...base,
    event_id: duplicateId,
    role: 'controller',
    source: 'runtime',
    event_type: 'runtime_status',
  }), /duplicate event_id/)
  assert.throws(() => reopened.append({
    ...base,
    sequence: 2,
    role: 'controller',
    source: 'runtime',
    event_type: 'runtime_status',
  }), /sequence must be 6/)
  rmSync(dir, { recursive: true, force: true })
}

// raw_done can only be recorded on an executor result from the environment.
{
  assert.throws(() => validateEvent({
    ...base,
    event_id: 'evt-x',
    sequence: 1,
    ts: 1,
    role: 'manager',
    source: 'shopper',
    event_type: 'shopper_qa',
    manager_question: 'q',
    shopper_reply: 'r',
    raw_done: true,
  }), /raw_done is only valid/)
  assert.throws(() => validateEvent({
    ...result('A', true),
    event_id: 'evt-y',
    sequence: 1,
    ts: 1,
    raw_result: { done: false },
  }), /must match raw_result.done/)
}

// Analyzer reports duplicate event IDs/sequences and missing results.
{
  const eventA = validateEvent({
    ...call('A'),
    event_id: 'same',
    sequence: 1,
    ts: 1,
  })
  const eventB = validateEvent({
    ...call('B'),
    event_id: 'same',
    sequence: 1,
    ts: 2,
  })
  const diagnostics = analyzeJournalCalls([eventA, eventB]).diagnostics
  assert.ok(diagnostics.some(item => item.kind === 'duplicate_event_id'))
  assert.ok(diagnostics.some(item => item.kind === 'duplicate_sequence'))
  assert.ok(diagnostics.some(item => item.kind === 'missing_result'))
}

// Analyzer rejects a result that tries to satisfy a call from another episode.
{
  const eventA = validateEvent({ ...call('A'), event_id: 'call-a', sequence: 1, ts: 1 })
  const eventB = validateEvent({
    ...result('A'),
    episode_id: 'episode-2',
    event_id: 'result-a',
    sequence: 2,
    ts: 2,
  })
  const diagnostics = analyzeJournalCalls([eventA, eventB]).diagnostics
  assert.ok(diagnostics.some(item => item.kind === 'call_identity_mismatch'))
  assert.ok(diagnostics.some(item => item.kind === 'missing_result'))
}

// Status dimensions and resume preconditions remain separate.
{
  const done = classifyRunStatus({
    envDone: true,
    harnessDone: true,
    managerDecision: 'done',
  })
  assert.equal(done.kind, 'done')
  assert.equal(done.environment_done, true)
  assert.equal(done.harness_outcome, 'done')
  assert.equal(classifyRunStatus({
    envDone: true,
    harnessDone: true,
    error: 'overload',
  }).kind, 'role_error')
  assert.equal(classifyRunStatus({
    managerDecision: 'blocked',
  }).kind, 'blocked')

  const resumable = {
    attemptComplete: true,
    fingerprintMatch: true,
    journalOk: true,
    environmentIdentityVerified: true,
    unauditedChanges: false,
  }
  assert.equal(canResume(resumable), true)
  assert.equal(canResume({ ...resumable, environmentIdentityVerified: false }), false)
  assert.equal(canResume({ ...resumable, unauditedChanges: true }), false)
  assert.equal(checkRecoveryPreconditions({
    journalDiagnostics: [{ kind: 'unknown_purchase_response' }],
    environmentIdentity: 'verified',
    shopperIdentity: 'verified',
  }).recovery_required, true)
  assert.equal(checkRecoveryPreconditions({
    journalDiagnostics: [{ kind: 'lease_mismatch' }],
    environmentIdentity: 'verified',
    shopperIdentity: 'verified',
  }).environment_identity_verified, false)
  assert.equal(checkRecoveryPreconditions({
    journalDiagnostics: [],
  }).environment_identity_verified, false)
  assert.equal(checkRecoveryPreconditions({
    journalDiagnostics: [],
    environmentIdentity: 'verified',
    shopperIdentity: 'verified',
  }).recovery_required, false)
}

console.log('test_mea_v4_runtime.mjs: all assertions passed')
