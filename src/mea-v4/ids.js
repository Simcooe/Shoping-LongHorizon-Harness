/** mea-v4 identity, path and stable input-fingerprint helpers (stage 1). */

import { createHash } from 'node:crypto'
import { join } from 'node:path'

export const SESSION_KEY_SCHEME_EXPLICIT = 'explicit-v1'
export const SESSION_KEY_SCHEME_LEGACY_TASK = 'legacy-task-idx'
export const SESSION_KEY_SCHEME_LEGACY_ENV = 'legacy-env-idx'
export const SESSION_KEY_SCHEME_NONE = 'none'

function hasValue(value) {
  return value !== undefined && value !== null && String(value).trim() !== ''
}

function required(value, field) {
  if (!hasValue(value)) throw new Error(`${field} is required`)
  return String(value).trim()
}

function safeSegment(value, field) {
  const segment = required(value, field)
  if (segment === '.' || segment === '..' || segment.includes('/') || segment.includes('\\')) {
    throw new Error(`${field} must be a safe path segment`)
  }
  return segment
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize)
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value).sort().map(key => [key, canonicalize(value[key])]),
    )
  }
  return value
}


export function resolveShopperSessionKey(config = {}, env = process.env) {
  const explicit = config?.sessionKey ?? env.SHOPPER_SESSION_KEY
  if (hasValue(explicit)) {
    return { key: String(explicit).trim(), scheme: SESSION_KEY_SCHEME_EXPLICIT }
  }
  const taskIdx = config?.taskIdx ?? env.SHOPSIM_TASK_IDX
  if (hasValue(taskIdx)) return { key: String(taskIdx), scheme: SESSION_KEY_SCHEME_LEGACY_TASK }
  const envIdx = config?.envIdx ?? env.SHOPSIM_ENV_IDX
  if (hasValue(envIdx)) return { key: `env-${envIdx}`, scheme: SESSION_KEY_SCHEME_LEGACY_ENV }
  return { key: '', scheme: SESSION_KEY_SCHEME_NONE }
}

export function runnerPreheatSessionKey(runId, taskIdx) {
  if (hasValue(runId) && hasValue(taskIdx)) return `${runId}/${taskIdx}`
  return hasValue(taskIdx) ? String(taskIdx) : ''
}

export function legacyAskSessionKey(taskIdx, envIdx) {
  if (hasValue(taskIdx)) return String(taskIdx)
  if (hasValue(envIdx)) return `env-${envIdx}`
  return ''
}

export function attemptSessionKey(runId, taskIdx, attemptId) {
  return `${required(runId, 'runId')}/${required(taskIdx, 'taskIdx')}`
    + `#attempt-${required(attemptId, 'attemptId')}`
}

export function executionIdentity({
  taskId, runId, attemptId, roundId = null, episodeId = null,
  environmentSession, shopperSession,
}) {
  return {
    task_id: required(taskId, 'taskId'),
    run_id: required(runId, 'runId'),
    attempt_id: required(attemptId, 'attemptId'),
    round_id: roundId === null ? null : required(roundId, 'roundId'),
    episode_id: episodeId === null ? null : required(episodeId, 'episodeId'),
    environment_session: required(environmentSession, 'environmentSession'),
    shopper_session: required(shopperSession, 'shopperSession'),
  }
}

export function episodeLogDir(runDir, role, episodeId) {
  return join(String(runDir), safeSegment(role, 'role'), safeSegment(episodeId, 'episodeId'))
}

export function journalPath(runDir, taskId) {
  return join(String(runDir), 'journal', `${safeSegment(taskId, 'taskId')}.jsonl`)
}

/** Stable logical-input fingerprint. Execution/lease identity is checked separately. */
export function inputFingerprint({
  taskId,
  benchmarkId,
  query,
  profile,
  profileConfigHash,
  stateSchema,
  journalSchema,
  environmentVersion,
  modelConfigs,
  budgetConfig,
  shopperSessionScheme,
}) {
  const canonical = canonicalize({
    taskId: required(taskId, 'taskId'),
    benchmarkId: required(benchmarkId, 'benchmarkId'),
    query: required(query, 'query'),
    profile: required(profile, 'profile'),
    profileConfigHash: required(profileConfigHash, 'profileConfigHash'),
    stateSchema: required(stateSchema, 'stateSchema'),
    journalSchema: required(journalSchema, 'journalSchema'),
    environmentVersion: required(environmentVersion, 'environmentVersion'),
    modelConfigs: modelConfigs ?? null,
    budgetConfig: budgetConfig ?? null,
    shopperSessionScheme: required(shopperSessionScheme, 'shopperSessionScheme'),
  })
  return createHash('sha256').update(JSON.stringify(canonical)).digest('hex')
}
