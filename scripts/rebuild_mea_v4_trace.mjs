#!/usr/bin/env node
/** Rebuild a mea-v4 task journal from archived Executor DSH sessions. */

import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { join } from 'node:path'
import { createJournal } from '../src/mea-v4/journal.js'
import { exportTaskJournal } from '../src/mea-v4/exporter.js'

const attemptDir = process.argv[2]
const tracesDir = process.argv[3]
if (!attemptDir || !tracesDir) throw new Error('usage: node scripts/rebuild_mea_v4_trace.mjs <attempt-dir> <traces-dir>')
const report = JSON.parse(readFileSync(join(attemptDir, 'controller-report.json'), 'utf8'))
const oldEvents = existsSync(join(attemptDir, 'journal.jsonl'))
  ? readFileSync(join(attemptDir, 'journal.jsonl'), 'utf8').split(/\r?\n/).filter(Boolean).map(JSON.parse) : []
const nonExecutor = oldEvents.filter(event => event.role !== 'executor')
  .sort((a, b) => a.sequence - b.sequence)
const sessionFiles = []
function walk(path) {
  for (const entry of readdirSync(path, { withFileTypes: true })) {
    const child = join(path, entry.name)
    if (entry.isDirectory()) walk(child)
    else if (entry.name === 'session.jsonl.zstd') sessionFiles.push(child)
  }
}
walk(join(attemptDir, 'executor'))
const episodeById = new Map(report.episodes.map(item => [item.episode_id, item]))
const reconstructed = []
for (const sessionPath of sessionFiles) {
  const episodeId = sessionPath.match(/executor\/(episode-[^/]+)/)?.[1]
  const episode = episodeById.get(episodeId)
  if (!episode) continue
  const roundId = episode.report.round_id
  const raw = execFileSync('zstd', ['-dc', sessionPath], { encoding: 'utf8', maxBuffer: 100 * 1024 * 1024 })
  const calls = new Map()
  for (const line of raw.split(/\r?\n/).filter(Boolean)) {
    let item; try { item = JSON.parse(line) } catch { continue }
    const data = item.data ?? {}
    if (item.type === 'tool/call') {
      let args = data.arguments
      try { args = typeof args === 'string' ? JSON.parse(args) : args } catch {}
      calls.set(data.callId, { local_step: data.step ?? 0, tool_name: data.name, tool_arguments: args ?? {} })
    } else if (item.type === 'tool/result') {
      const callId = data.message?.source?.callId ?? data.message?.content?.[0]?.toolCallId
      const call = calls.get(callId)
      if (!call) continue
      const meta = data.meta ?? data.message?.meta ?? {}
      const text = (data.message?.content ?? []).flatMap(block => block.content ?? [])
        .filter(block => block?.type === 'text').map(block => block.text ?? '').join('\n')
      reconstructed.push({ episode_id: episodeId, round_id: roundId, call_id: callId,
        ...call, raw_result: meta.raw ?? {}, model_visible_text: text })
    }
  }
}
const target = join(attemptDir, 'journal.rebuilt.jsonl')
writeFileSync(target, '')
const journal = createJournal(target)
for (const event of nonExecutor) {
  const { schema, event_id, sequence, ts, ...payload } = event
  journal.append(payload)
}
for (const episode of report.episodes) {
  const episodeId = episode.episode_id, roundId = episode.report.round_id
  journal.append({ task_id: report.task_id, run_id: report.run_id, attempt_id: report.attempt_id,
    round_id: roundId, episode_id: episodeId, role: 'executor', source: 'runtime', event_type: 'episode_start' })
  for (const item of reconstructed.filter(value => value.episode_id === episodeId)) {
    journal.append({ task_id: report.task_id, run_id: report.run_id, attempt_id: report.attempt_id,
      round_id: roundId, episode_id: episodeId, role: 'executor', source: 'executor', event_type: 'tool_call',
      local_step: item.local_step, call_id: item.call_id, tool_call_id: item.call_id,
      tool_name: item.tool_name, tool_arguments: item.tool_arguments })
    journal.append({ task_id: report.task_id, run_id: report.run_id, attempt_id: report.attempt_id,
      round_id: roundId, episode_id: episodeId, role: 'executor', source: 'environment', event_type: 'tool_result',
      local_step: item.local_step, call_id: item.call_id, raw_result: item.raw_result,
      model_visible_text: item.model_visible_text,
      ...(typeof item.raw_result?.done === 'boolean' ? { raw_done: item.raw_result.done, env_done: item.raw_result.done } : {}) })
  }
  journal.append({ task_id: report.task_id, run_id: report.run_id, attempt_id: report.attempt_id,
    round_id: roundId, episode_id: episodeId, role: 'executor', source: 'runtime', event_type: 'episode_end',
    episode_status: episode.runtime_status })
}
journal.close()
const out = exportTaskJournal(target, tracesDir, { id: report.task_id, task: report.state.task.original_goal })
console.log(JSON.stringify({ journal: target, model_trace: out.model_path, raw_trace: out.raw_path,
  reconstructed_results: reconstructed.length }))
