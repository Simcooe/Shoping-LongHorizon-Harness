/**
 * mea-loop — MEA (Memory-Episode-Action) runtime control plugin（第一版最小在线 loop）。
 *
 * 不是另一个购物工具，也不是另一个模型 Agent。它是 DSH 中的运行时控制插件：
 *
 *   用户初始公开需求
 *     → 初始化 Task State
 *     → 注入 Round 1 contract
 *     → 购物 Executor 使用现有工具（search/click/finish/ask_shopper）
 *     → mea_round_report
 *     → 更新 Task State 并返回下一轮 contract
 *     → 注入 Round 2 / Round 3 / ...
 *     → 最终通过 Episode finished / finish 结束环境
 *
 * 第一版边界（详见 docs/prompts/MEA_LOOP_MINIMAL_RUNTHROUGH.md）：
 *   - State 只保存原始公开信息：初始 query 原文、ask_shopper question/reply 原文、
 *     实时 tool name/arguments、返回给模型的同一份 result.value.text。
 *   - 不读取 result.value.state / raw / presentationMeta / reward / goal / gold。
 *   - tools/pre-execute 只做工具调用计数，不因 allowedTools / round budget /
 *     Buy Now / 页面类型 / 规格状态 拒绝购物工具。
 *   - round 用固定阶段：discover → inspect → resolve → act+。
 *
 * @module @shopping-longhorizon/shop-tools/mea-loop
 */

import { mkdirSync, writeFileSync, renameSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const name = 'mea-loop'
export const inject = ['tools']

/** 当前版本 schema。 */
export const STATE_SCHEMA = 'shopping-mea-state-v1'

/** 购物工具（属于 MEA 观察与计数范围的工具）。 */
const SHOP_TOOLS = new Set(['search', 'click', 'finish', 'ask_shopper'])

const TERMINAL_RE = /Episode finished\./

/** 固定阶段。 */
export function stageForRound(number) {
  if (number === 1) return 'discover'
  if (number === 2) return 'inspect'
  if (number === 3) return 'resolve'
  return 'act'
}

/** 固定阶段目标（确定性，非 LLM Manager）。 */
export function goalForStage(stage) {
  switch (stage) {
    case 'discover':
      return '根据用户原始需求搜索候选商品。'
    case 'inspect':
      return '打开一个候选，查看商品、规格和价格信息。'
    case 'resolve':
      return '根据用户需求和当前证据继续核验；必要时使用 ask_shopper。'
    case 'act':
      return '根据当前 Task State 继续搜索、检查、澄清或购买。'
    default:
      return '继续购物任务。'
  }
}

/** 建议工具（只是给模型看的提示，不硬拦截）。 */
export function suggestedTools(stage, hasShopper = true) {
  switch (stage) {
    case 'discover':
      return ['search', 'click']
    case 'inspect':
      return ['click']
    case 'resolve':
      return hasShopper ? ['click', 'ask_shopper', 'search'] : ['click', 'search']
    case 'act':
      return hasShopper ? ['search', 'click', 'ask_shopper', 'finish'] : ['search', 'click', 'finish']
    default:
      return ['search', 'click', 'finish']
  }
}

/** 最小预算解析（保留，但第一版不依赖它推进 round）。 */
export function parseBudget(text) {
  if (typeof text !== 'string') return null
  const hard = text.match(/(?:最多|不超过|不高于|上限|最高|至多)\s*(?:接受|给|出|花|付|买|要|只能|可以)?\s*(\d+(?:\.\d+)?)\s*元?/)
  const within = text.match(/(\d+(?:\.\d+)?)\s*元?\s*(?:以内|以下|之内|封顶)/)
  const approx = text.match(/(\d+(?:\.\d+)?)\s*元?\s*(?:左右|上下)/)
  const m = hard ?? within ?? approx
  if (!m) return null
  const upper = Number(m[1])
  if (!Number.isFinite(upper)) return null
  return { upper, approximate: hard === null && within === null && approx !== null }
}

function firstTextBlock(content) {
  if (!Array.isArray(content)) return ''
  let out = ''
  for (const block of content) {
    if (block && block.type === 'text' && typeof block.text === 'string') out += block.text
  }
  return out
}

/** 从 ask_shopper 的模型可见 text（"用户回复：..."）提取回复原文。 */
export function extractReply(text) {
  if (typeof text !== 'string') return ''
  const m = text.match(/^用户回复[：:]\s*([\s\S]*)$/)
  return m ? m[1] : text
}

function truncate(text, max = 1200) {
  const s = typeof text === 'string' ? text : ''
  if (s.length <= max) return s
  return `${s.slice(0, max)} …（截断）`
}

function makeRound(number) {
  const stage = stageForRound(number)
  return {
    number,
    stage,
    goal: goalForStage(stage),
    summary: '',
    toolCalls: 0,
    status: 'running',
  }
}

/** 从任务身份 + 公开 query 构造最小 State。 */
export function initializeFromTask({ runId, taskId, envIdx, envSession, query }) {
  const round = makeRound(1)
  return {
    schema: STATE_SCHEMA,
    task: {
      runId: runId ?? '',
      taskId: taskId ?? '',
      envIdx: envIdx ?? '',
      envSession: envSession ?? '',
    },
    objective: { query: query ?? '' },
    clarifications: [],
    currentObservation: {
      toolName: '',
      toolArguments: null,
      modelVisibleText: '',
    },
    rounds: [round],
    round,
    decision: { kind: 'running', reason: null },
  }
}

/**
 * 生成当前 Task State 的 round contract（模型上下文控制块）。
 * mea_round_report 的返回文本与 agent/pre-step 注入共用此函数。
 */
export function buildRoundContract(state) {
  const clar = state.clarifications ?? []
  const obs = state.currentObservation ?? {}
  const round = state.round ?? {}
  const rounds = state.rounds ?? []
  const prev = rounds.length >= 2 ? rounds[rounds.length - 2] : null
  const lines = []
  lines.push('MEA 当前状态：', '')
  lines.push('用户最初需求：')
  lines.push(state.objective?.query ?? '')
  lines.push('')
  lines.push('用户后续回复：')
  if (clar.length === 0) {
    lines.push('- 无')
  } else {
    for (const c of clar) lines.push(`- [Round ${c.round}] ${c.reply}`)
  }
  lines.push('')
  lines.push('上一轮结果：')
  lines.push(prev?.summary || '无')
  lines.push('')
  lines.push('当前可观察状态：')
  lines.push(`- 上一次工具：${obs.toolName || '(无)'}`)
  lines.push(`- 上一次工具参数：${JSON.stringify(obs.toolArguments ?? null)}`)
  if (obs.modelVisibleText) lines.push(`- 当前可见页面摘要：${truncate(obs.modelVisibleText)}`)
  lines.push('')
  lines.push('当前 Round：')
  lines.push(`- Round ${round.number}`)
  lines.push(`- 阶段：${round.stage}`)
  lines.push(`- 目标：${round.goal}`)
  lines.push(`- 当前工具调用次数：${round.toolCalls}`)
  lines.push(`- 建议工具：${suggestedTools(round.stage, true).join(', ')}`)
  lines.push('')
  lines.push('完成当前目标后调用 mea_round_report。')
  lines.push('mea_round_report 返回下一轮目标时必须继续执行。')
  lines.push('只有 Episode finished、finish 或 MEA 明确结束时才能输出 final。')
  return lines.join('\n')
}

/** 持久化目录：runtime 决定，不由模型参数决定。 */
export function resolveStateDir(config, env = process.env) {
  return config?.stateDir
    ?? env.MEA_STATE_DIR
    ?? (env.DSH_HOME ? join(env.DSH_HOME, 'mea') : join(process.cwd(), '.mea'))
}

function atomicWrite(file, value) {
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.tmp`
  writeFileSync(tmp, JSON.stringify(value, null, 2))
  renameSync(tmp, file)
}

function appendJsonl(file, value) {
  mkdirSync(dirname(file), { recursive: true })
  writeFileSync(file, `${JSON.stringify(value)}\n`, { flag: 'a' })
}

/**
 * DSH 插件入口。返回 { stateDir, ...internals } 便于 fixture 测试。
 */
export function apply(ctx, config = {}) {
  const stateDir = resolveStateDir(config)
  const maxRounds = Number(config?.maxRounds ?? 10)
  const hasShopper = Boolean(config?.shopperUrl ?? process.env.SHOPPER_BASE_URL)

  let state = null
  let readSequence = 0
  let evidenceSeq = 0
  let injectedRound = -1

  const persistState = () => {
    if (state) atomicWrite(join(stateDir, 'state.json'), state)
  }
  const persistRound = (round) => appendJsonl(join(stateDir, 'rounds.jsonl'), round)
  const persistEvidence = (evidence) => appendJsonl(join(stateDir, 'evidence.jsonl'), evidence)

  function ensureState(payload) {
    if (state) return
    const messages = Array.isArray(payload?.messages) ? payload.messages : []
    const userMsg = messages.find(m => m?.source?.kind === 'user')
    const text = firstTextBlock(userMsg?.content).trim()
    if (!text) return
    const session = payload?.agent?.session
    state = initializeFromTask({
      runId: String(session?.id ?? ''),
      taskId: process.env.SHOPSIM_TASK_IDX ?? '',
      envIdx: process.env.SHOPSIM_ENV_IDX ?? '',
      envSession: String(session?.id ?? ''),
      query: text,
    })
    persistState()
  }

  function observeTool(toolName, toolArguments, text) {
    readSequence += 1
    evidenceSeq += 1
    // 只保存实时 tool name / arguments / 返回给模型的同一份 text。
    state.currentObservation = {
      toolName,
      toolArguments: toolArguments ?? null,
      modelVisibleText: text,
    }

    if (toolName === 'ask_shopper') {
      const question = typeof toolArguments?.question === 'string' ? toolArguments.question : ''
      const reply = extractReply(text)
      state.clarifications.push({
        round: state.round.number,
        question,
        reply,
        evidenceRef: `ev-${evidenceSeq}`,
      })
    }

    persistEvidence({
      evidence_id: `ev-${evidenceSeq}`,
      round: state.round.number,
      source: 'live_tool_event',
      kind: 'tool_observation',
      read_sequence: readSequence,
      tool_name: toolName,
      tool_arguments: toolArguments ?? null,
      model_visible_text: text,
    })

    if (TERMINAL_RE.test(text)) {
      state.round.status = 'complete'
      state.decision = { kind: 'terminal', reason: 'episode_finished_observed' }
      persistState()
      return
    }
    if (toolName === 'finish') {
      state.round.status = 'complete'
      state.decision = { kind: 'finished', reason: 'agent_called_finish' }
      persistState()
      return
    }
    persistState()
  }

  function reportRound(summary) {
    if (!state) return { text: 'MEA: Task State 尚未初始化。' }
    if (state.decision.kind !== 'running') {
      return { text: `MEA: 任务已结束（${state.decision.kind}），无需继续。` }
    }
    // 1. 保存当前 round summary
    state.round.summary = summary
    state.round.status = 'complete'
    persistRound({
      round: state.round.number,
      stage: state.round.stage,
      goal: state.round.goal,
      summary,
      tool_calls: state.round.toolCalls,
      status: 'complete',
    })
    // 2. 按固定阶段创建下一轮
    const nextNumber = state.round.number + 1
    if (nextNumber > maxRounds) {
      state.decision = { kind: 'max_rounds', reason: `maxRounds ${maxRounds} reached` }
      persistState()
      return { text: `MEA: 已达到最大轮次 ${maxRounds}，任务结束。` }
    }
    const next = makeRound(nextNumber)
    state.rounds.push(next)
    state.round = next
    persistState()
    // 下一轮 contract 已经随 tool result 返回给模型，避免 agent/pre-step 再次重复注入。
    injectedRound = state.round.number
    return { text: buildRoundContract(state) }
  }

  // ── 观察 + 计数：实时购物工具（第一版不因 allowedTools/budget/Buy Now 拒绝）──
  ctx.on('tools/pre-execute', async (exec, next) => {
    if (!SHOP_TOOLS.has(exec.name)) return next()
    if (state && state.round.status === 'running') {
      state.round.toolCalls += 1
      persistState()
    }
    return next()
  })

  // ── 观察：实时工具结果，只消费 result.value.text ──
  ctx.on('tools/post-execute', async (exec, result, next) => {
    try {
      if (state && SHOP_TOOLS.has(exec.name) && !result.isError) {
        const text = typeof result.value?.text === 'string' ? result.value.text : ''
        observeTool(exec.name, exec.arguments, text)
      }
    } catch (error) {
      console.error('[mea-loop] post-execute observer failed:', error)
    }
    return next()
  })

  // ── 兜底初始化：session/event 也可能先于 pre-step 触发（resume 场景）──
  ctx.on('session/event', (session, event) => {
    if (state) return
    if (event?.type !== 'user/message') return
    if (event.data?.source?.kind !== 'user') return
    const text = firstTextBlock(event.data?.content).trim()
    if (!text) return
    state = initializeFromTask({
      runId: String(session?.id ?? ''),
      taskId: process.env.SHOPSIM_TASK_IDX ?? '',
      envIdx: process.env.SHOPSIM_ENV_IDX ?? '',
      envSession: String(session?.id ?? ''),
      query: text,
    })
    persistState()
  })

  // ── 上下文注入：第一次模型请求前注入 Round 1 contract ──
  ctx.on('agent/pre-step', async (payload, next) => {
    ensureState(payload)
    const decision = await next()
    if (!state || decision.kind !== 'enter') return decision
    if (state.round.number === injectedRound) return decision
    const text = buildRoundContract(state)
    const fresh = {
      role: 'user',
      content: [{ type: 'text', text }],
      source: {
        kind: 'plugin',
        plugin: 'mea-loop',
        form: 'snapshot',
        sections: [{ name: 'mea-round', text }],
      },
    }
    const kept = decision.messages.filter(
      m => !(m?.source?.kind === 'plugin' && m?.source?.plugin === 'mea-loop'),
    )
    injectedRound = state.round.number
    return { ...decision, messages: [...kept, fresh] }
  })

  // ── 受控工具：结束当前 round，返回下一轮 contract，绝不宣布 task success ──
  ctx.tools.register({
    name: 'mea_round_report',
    description:
      'End the CURRENT bounded MEA round with a one-line summary. It ends only the current round, never the shop episode, and never declares a purchase successful. The tool returns the next round goal; keep executing when it does.',
    parameters: {
      type: 'object',
      properties: {
        summary: { type: 'string', description: 'one-line summary of what this round did/observed' },
      },
      required: ['summary'],
      additionalProperties: false,
    },
    async execute(args) {
      return reportRound(String(args?.summary ?? ''))
    },
    output: {
      schema: {
        type: 'object',
        properties: { text: { type: 'string' } },
        required: ['text'],
        additionalProperties: false,
      },
      render: (_args, value) => [{ type: 'text', text: value.text }],
    },
  })

  return {
    stateDir,
    __internals: {
      get state() { return state },
      get readSequence() { return readSequence },
    },
  }
}
