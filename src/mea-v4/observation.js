/** Trusted runtime registry for read-only observations used by Audit evidence. */

export const OBSERVATION_SCHEMA = 'longhorizon-observation-v1'

function nonEmpty(value, field) {
  if (typeof value !== 'string' || value.length === 0) throw new Error(`${field} is required`)
  return value
}

export function createObservationRegistry(initial = []) {
  const entries = new Map()
  const register = observation => {
    if (!observation || typeof observation !== 'object' || Array.isArray(observation)) {
      throw new Error('observation must be an object')
    }
    const normalized = {
      schema: OBSERVATION_SCHEMA,
      event_id: nonEmpty(observation.event_id, 'observation.event_id'),
      snapshot_id: nonEmpty(observation.snapshot_id, 'observation.snapshot_id'),
      environment_session: nonEmpty(
        observation.environment_session, 'observation.environment_session'),
      read_only: observation.read_only === true,
      source: nonEmpty(observation.source, 'observation.source'),
      scope: observation.scope ?? null,
      raw_ref: nonEmpty(observation.raw_ref, 'observation.raw_ref'),
    }
    if (!normalized.read_only) throw new Error('audit observation must be read-only')
    if (entries.has(normalized.event_id)) throw new Error(`duplicate observation "${normalized.event_id}"`)
    entries.set(normalized.event_id, normalized)
    return { ...normalized }
  }
  for (const item of initial) register(item)
  return {
    register,
    resolve(ref) {
      const observation = entries.get(ref.event_id)
      if (!observation) throw new Error(`unknown observation "${ref.event_id}"`)
      if (observation.snapshot_id !== ref.snapshot_id
        || observation.environment_session !== ref.environment_session) {
        throw new Error(`observation identity mismatch for "${ref.event_id}"`)
      }
      return { ...observation }
    },
  }
}
