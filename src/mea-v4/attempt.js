/** Durable mea-v4 attempt manifest/checkpoint helpers (stage 1). */

import {
  existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync,
} from 'node:fs'
import { dirname, join } from 'node:path'

export const ATTEMPT_SCHEMA = 'longhorizon-attempt-v1'
export const CHECKPOINT_SCHEMA = 'longhorizon-checkpoint-v1'

const TERMINAL_RUNTIME_STATUSES = new Set(['completed', 'failed', 'interrupted'])

function nonEmpty(value, field) {
  if (typeof value !== 'string' || value.trim() === '') throw new Error(`${field} is required`)
  return value.trim()
}

function atomicWriteJson(path, value) {
  mkdirSync(dirname(path), { recursive: true })
  const temporary = `${path}.tmp-${process.pid}`
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`)
  renameSync(temporary, path)
}

export function attemptDir(runDir, taskId, attemptId) {
  return join(String(runDir), 'tasks', nonEmpty(String(taskId), 'taskId'), 'attempts',
    nonEmpty(String(attemptId), 'attemptId'))
}

export function createAttemptManifest({
  taskId, runId, attemptId, inputFingerprint,
  environmentSession, environmentLease = null, shopperSession,
  journal = 'journal.jsonl', checkpoint = 'checkpoint.json', episodes = 'episodes',
}) {
  return validateAttemptManifest({
    schema: ATTEMPT_SCHEMA,
    task_id: nonEmpty(String(taskId), 'taskId'),
    run_id: nonEmpty(runId, 'runId'),
    attempt_id: nonEmpty(attemptId, 'attemptId'),
    input_fingerprint: nonEmpty(inputFingerprint, 'inputFingerprint'),
    identity: {
      environment_session: nonEmpty(environmentSession, 'environmentSession'),
      environment_lease: environmentLease,
      shopper_session: nonEmpty(shopperSession, 'shopperSession'),
    },
    artifacts: { journal, checkpoint, episodes },
    runtime_status: 'initialized',
    protocol_valid: false,
    export_ok: false,
    session_ok: false,
    attempt_complete: false,
    unaudited_changes: true,
    created_at: Date.now(),
    updated_at: Date.now(),
    error: null,
    replaced_attempt_id: null,
  })
}

export function validateAttemptManifest(manifest) {
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    throw new Error('attempt manifest must be an object')
  }
  if (manifest.schema !== ATTEMPT_SCHEMA) throw new Error('unsupported attempt schema')
  for (const field of ['task_id', 'run_id', 'attempt_id', 'input_fingerprint']) {
    nonEmpty(manifest[field], `manifest.${field}`)
  }
  if (!manifest.identity || typeof manifest.identity !== 'object') {
    throw new Error('manifest.identity is required')
  }
  nonEmpty(manifest.identity.environment_session, 'manifest.identity.environment_session')
  nonEmpty(manifest.identity.shopper_session, 'manifest.identity.shopper_session')
  if (!manifest.artifacts || typeof manifest.artifacts !== 'object') {
    throw new Error('manifest.artifacts is required')
  }
  for (const field of ['journal', 'checkpoint', 'episodes']) {
    nonEmpty(manifest.artifacts[field], `manifest.artifacts.${field}`)
  }
  if (!['initialized', 'running', ...TERMINAL_RUNTIME_STATUSES].includes(manifest.runtime_status)) {
    throw new Error(`invalid runtime_status "${manifest.runtime_status}"`)
  }
  for (const field of ['protocol_valid', 'export_ok', 'session_ok', 'attempt_complete', 'unaudited_changes']) {
    if (typeof manifest[field] !== 'boolean') throw new Error(`manifest.${field} must be boolean`)
  }
  if (manifest.attempt_complete && !TERMINAL_RUNTIME_STATUSES.has(manifest.runtime_status)) {
    throw new Error('complete attempt requires terminal runtime_status')
  }
  return JSON.parse(JSON.stringify(manifest))
}

export function writeAttemptManifest(path, manifest) {
  const normalized = validateAttemptManifest({ ...manifest, updated_at: Date.now() })
  atomicWriteJson(path, normalized)
  return normalized
}

export function readAttemptManifest(path) {
  return validateAttemptManifest(JSON.parse(readFileSync(path, 'utf8')))
}

export function updateAttemptManifest(path, patch) {
  const current = readAttemptManifest(path)
  return writeAttemptManifest(path, { ...current, ...patch, identity: current.identity, artifacts: current.artifacts })
}

export function writeCheckpoint(path, { sequence, stateHash, unauditedChanges, environmentDone }) {
  if (!Number.isInteger(sequence) || sequence < 0) throw new Error('checkpoint.sequence is invalid')
  const checkpoint = {
    schema: CHECKPOINT_SCHEMA,
    sequence,
    state_hash: nonEmpty(stateHash, 'stateHash'),
    unaudited_changes: Boolean(unauditedChanges),
    environment_done: Boolean(environmentDone),
    written_at: Date.now(),
  }
  atomicWriteJson(path, checkpoint)
  return checkpoint
}

export function assessCompletedAttempt(manifest, { fingerprint, journalDiagnostics = [] } = {}) {
  const normalized = validateAttemptManifest(manifest)
  const reasons = []
  if (!normalized.attempt_complete) reasons.push('attempt_incomplete')
  if (normalized.runtime_status !== 'completed') reasons.push('runtime_not_completed')
  if (!normalized.protocol_valid) reasons.push('protocol_invalid')
  if (!normalized.export_ok) reasons.push('export_failed')
  if (!normalized.session_ok) reasons.push('session_missing')
  if (normalized.unaudited_changes) reasons.push('unaudited_changes')
  if (fingerprint !== undefined && normalized.input_fingerprint !== fingerprint) {
    reasons.push('fingerprint_mismatch')
  }
  if (journalDiagnostics.length > 0) reasons.push('journal_invalid')
  return { can_skip: reasons.length === 0, reasons }
}

export function findCompletedAttempt(taskAttemptsDir, options = {}) {
  if (!existsSync(taskAttemptsDir)) return null
  for (const entry of readdirSync(taskAttemptsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    const path = join(taskAttemptsDir, entry.name, 'manifest.json')
    if (!existsSync(path)) continue
    try {
      const manifest = readAttemptManifest(path)
      if (assessCompletedAttempt(manifest, options).can_skip) return { path, manifest }
    } catch {
      // Invalid attempts are retained but never treated as resume hits.
    }
  }
  return null
}
