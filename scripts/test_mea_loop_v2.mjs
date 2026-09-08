// mea-loop v2 Manager LLM fixture/mock 测试。
//
// 通过真实 ctx.llm.stream 调用形状验证 Manager 行为，不发起真实模型请求。
// mock 使用 scripted stream（text-delta + finish 块），记录每次调用的 options。
//
// 运行：node scripts/test_mea_loop_v2.mjs
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  apply,
  parseManagerOutput,
  initializeFromTask,
} from '../src/mea-loop.js'

function textChunks(text) {
  return [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'finish', reason: { kind: 'stop' } },
  ]
}

const EXECUTE_1 = {
  decision: 'execute',
  state_summary: '需要找到带红色手柄的排气阀候选',
  open_gaps: ['找到候选商品'],
  reason: '开始任务，先搜索候选',
  next_round: {
    goal: '搜索红色手柄水暖排气阀候选',
    suggested_tools: ['search', 'click'],
    max_tool_calls: 5,
    completion_criteria: ['找到至少一个候选'],
  },
}

const EXECUTE_2 = {
  decision: 'execute',
  state_summary: '候选 A 已打开，需确认规格与价格',
  open_gaps: ['核验规格和价格'],
  reason: '上一轮找到了候选，本轮核验',
  next_round: {
    goal: '打开候选 A 核验规格与价格',
    suggested_tools: ['click'],
    max_tool_calls: 4,
    completion_criteria: ['看到价格和规格'],
  },
}

const BLOCKED = {
  decision: 'blocked',
  state_summary: '已检查多个候选但公开页面都没有包装数量',
  open_gaps: ['无法核验是否满足100根'],
  reason: '没有剩余公开动作能够解决数量缺口',
  next_round: null,
}

const MOCK_EXECUTOR_TOOLS = [
  {
    name: 'search',
    description: 'Search the shop for products.',
    parameters: { type: 'object', properties: { keywords: { type: 'string' } }, required: ['keywords'] },
  },
  {
    name: 'click',
    description: 'Click a value on the current page.',
    parameters: { type: 'object', properties: { value: { type: 'string' } }, required: ['value'] },
  },
  {
    name: 'finish',
    description: 'End the episode without purchasing (this is not a success).',
    parameters: { type: 'object', properties: { reason: { enum: ['no_suitable_product'] } }, required: ['reason'] },
  },
  {
    name: 'ask_shopper',
    description: 'Ask the user one clarifying question and get their reply.',
    parameters: { type: 'object', properties: { question: { type: 'string' } }, required: ['question'] },
  },
  {
    name: 'mea_round_report',
    description: 'End the current bounded MEA round.',
    parameters: { type: 'object', properties: { summary: { type: 'string' } }, required: ['summary'] },
  },
]

function makeCtx() {
  const handlers = new Map()
  const tools = []
  const llmCalls = []
  const scripts = []
  const ctx = {
    on(event, fn) {
      if (!handlers.has(event)) handlers.set(event, [])
      handlers.get(event).push(fn)
      return () => {}
    },
    async fire(event, ...args) {
      const list = handlers.get(event) ?? []
      let idx = list.length - 1
      const makeNext = () => {
        if (idx < 0) return async () => {}
        const fn = list[idx]
        idx -= 1
        return () => fn(...args, makeNext())
      }
      if (list.length === 0) return undefined
      return list[list.length - 1](...args, makeNext())
    },
    tools: {
      register(t) { tools.push(t) },
      schemas() { return MOCK_EXECUTOR_TOOLS },
    },
    llm: {
      pushScript(script) { scripts.push(script) },
      async *stream(options) {
        llmCalls.push(options)
        const script = scripts.shift()
        if (!script) throw new Error('mock llm: no script queued')
        if (typeof script === 'function') {
          yield* script(options)
        } else {
          yield* script
        }
      },
    },
  }
  return { ctx, handlers, tools, llmCalls }
}

function userMessage(text) {
  return { source: { kind: 'user' }, content: [{ type: 'text', text }] }
}

function stateFile(dir) {
  return JSON.parse(readFileSync(join(dir, 'state.json'), 'utf8'))
}

async function fireFirstPreStep(ctx, dir, query, scripts) {
  for (const s of scripts) ctx.llm.pushScript(s)
  const injected = await ctx.fire('agent/pre-step',
    { agent: { session: { id: 'session-v2' } }, messages: [userMessage(query)], turn: 1, step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [userMessage(query)] }))
  return injected
}

// ── parseManagerOutput 单测 ──
{
  const p = parseManagerOutput(JSON.stringify(EXECUTE_1))
  assert.equal(p.decision, 'execute')
  assert.equal(p.nextRound.goal, '搜索红色手柄水暖排气阀候选')

  // code fence 最小清洗
  const fenced = parseManagerOutput('```json\n' + JSON.stringify(EXECUTE_2) + '\n```')
  assert.equal(fenced.decision, 'execute')

  // 非法 tool name 拒绝
  assert.throws(() => parseManagerOutput(JSON.stringify({
    decision: 'execute', state_summary: 'x', open_gaps: [], reason: 'r',
    next_round: { goal: 'g', suggested_tools: ['buy'], max_tool_calls: 3, completion_criteria: ['c'] },
  })), /available tool/)

  // 未知字段拒绝
  assert.throws(() => parseManagerOutput(JSON.stringify({ decision: 'execute', state_summary: 'x', open_gaps: [], reason: 'r', hidden: 1, next_round: null })), /unknown top-level/)

  // blocked 必须 next_round null
  assert.throws(() => parseManagerOutput(JSON.stringify({ ...BLOCKED, next_round: { goal: 'x' } })), /requires next_round to be null/)
}

// ── 主流程：Manager 在第一次 Executor 前调用，report 后 fresh 调用 ──
async function runMain() {
  const dir = mkdtempSync(join(tmpdir(), 'mea-loop-v2-'))
  const { ctx, tools, llmCalls } = makeCtx()
  apply(ctx, { stateDir: dir, plannerMode: 'llm', maxRounds: 10, manager: { provider: 'mock', model: 'mock-m' } })

  const QUERY = '我想买一个红色手柄的水暖排气阀，价格30元左右。'
  const injected = await fireFirstPreStep(ctx, dir, QUERY, [
    textChunks(JSON.stringify(EXECUTE_1)),
  ])

  // 16.1 Manager 在第一次 Executor 请求前调用
  assert.equal(llmCalls.length, 1, 'Manager 应在第一次 Executor 请求前调用')
  // 16.2 Manager 收到完整初始 Query
  const input1 = JSON.parse(llmCalls[0].messages[0].content[0].text)
  assert.equal(input1.task.initial_request, QUERY)

  // ── 新增：Manager 输入携带真实 Executor tool schemas（只读）──
  assert.ok(
    input1.runtime.executor_tools.some(
      tool => tool.name === 'finish' && tool.description.includes('without purchasing'),
    ),
    'executor_tools 应包含 finish 的真实 description',
  )
  assert.deepEqual(
    input1.runtime.available_tools,
    input1.runtime.executor_tools.map(tool => tool.name),
    'available_tools 必须从 executor_tools 派生',
  )
  assert.equal(
    input1.runtime.executor_tools.some(tool => tool.name === 'mea_round_report'),
    false,
    'mea_round_report 不得进入 Manager 可规划的购物工具集合',
  )
  assert.equal(
    input1.runtime.shopper_available,
    true,
    '存在 ask_shopper schema 时 shopper_available 应为 true',
  )
  // Manager 不能真正调用工具
  assert.equal(llmCalls[0].tools, undefined, 'Manager 调用不得传 tools')
  // 16.3 不含 state/raw/reward/gold
  const input1Str = JSON.stringify(llmCalls[0])
  assert.ok(!input1Str.includes('"reward"'))
  assert.ok(!input1Str.includes('"gold"'))
  assert.ok(!input1Str.includes('"raw"'))
  assert.ok(!input1Str.includes('presentationMeta'))
  // 16.4 Round 1 来自 Manager 输出，不是 stageForRound(1)
  assert.ok(injected.messages.some(m => m.source?.plugin === 'mea-loop'))
  const contract1 = injected.messages.find(m => m.source?.plugin === 'mea-loop').content[0].text
  assert.ok(contract1.includes('搜索红色手柄水暖排气阀候选'))
  assert.ok(!contract1.includes('discover'), 'v2 不得使用固定阶段名 discover')

  let s = stateFile(dir)
  assert.equal(s.round.number, 1)
  assert.equal(s.round.goal, '搜索红色手柄水暖排气阀候选')
  assert.equal(s.manager.calls, 1)
  assert.equal(s.manager.lastDecision, 'execute')

  // 模拟一轮工具事件
  await ctx.fire('tools/pre-execute', { name: 'search', arguments: { keywords: '排气阀' } }, async () => ({ kind: 'allow' }))
  await ctx.fire('tools/post-execute',
    { name: 'search', arguments: { keywords: '排气阀' } },
    { isError: false, value: { text: '搜索结果\n可点击的按钮: ["123456789012"]', state: { reward: 1, gold: '999' }, raw: { gold: '999' } } },
    async () => ({ kind: 'accept' }))

  // 第一次 mea_round_report → fresh Manager request
  const report = tools.find(t => t.name === 'mea_round_report')
  ctx.llm.pushScript(textChunks(JSON.stringify(EXECUTE_2)))
  const r1 = await report.execute({ summary: '找到候选 123456789012' })
  assert.equal(llmCalls.length, 2, '第一次 report 后应发起新的 Manager 请求')
  // 16.5/16.8 两次 request 不共享 messages 历史（fresh one-shot）
  assert.equal(llmCalls[0].messages.length, 1)
  assert.equal(llmCalls[1].messages.length, 1)
  assert.notEqual(llmCalls[0].messages[0].content[0].text, llmCalls[1].messages[0].content[0].text)
  // 16.6 第二次 Manager 输入包含上一轮 report 和该轮公开 events
  const input2 = JSON.parse(llmCalls[1].messages[0].content[0].text)
  assert.equal(input2.last_round.executor_report, '找到候选 123456789012')
  assert.equal(input2.last_round.tool_events.length, 1)
  assert.equal(input2.last_round.tool_events[0].tool_name, 'search')
  // events 只含公开 text，不含诱导性 reward/gold
  assert.ok(!JSON.stringify(input2).includes('999'))
  assert.ok(r1.text.includes('核验规格与价格'), 'report 返回下一轮 contract')

  s = stateFile(dir)
  assert.equal(s.round.number, 2)
  assert.equal(s.rounds.length, 2)

  // ask_shopper → 原始回复进入下一次 Manager 输入
  await ctx.fire('tools/post-execute',
    { name: 'ask_shopper', arguments: { question: '接口口径？' } },
    { isError: false, value: { text: '用户回复：我要6分接口，全铜材质。' } },
    async () => ({ kind: 'accept' }))

  ctx.llm.pushScript(textChunks(JSON.stringify(EXECUTE_2)))
  await report.execute({ summary: '澄清了接口口径' })
  assert.equal(llmCalls.length, 3)
  const input3 = JSON.parse(llmCalls[2].messages[0].content[0].text)
  assert.equal(input3.task.clarifications[0].reply, '我要6分接口，全铜材质。')

  // 16.9 execute → 创建新 round
  s = stateFile(dir)
  assert.equal(s.round.number, 3)
  assert.equal(s.rounds.length, 3)

  // 16.10 blocked → 停止创建新 round
  ctx.llm.pushScript(textChunks(JSON.stringify(BLOCKED)))
  const rb = await report.execute({ summary: '无进展' })
  s = stateFile(dir)
  assert.equal(s.decision.kind, 'blocked')
  assert.equal(s.rounds.length, 3, 'blocked 后不再创建新 round')
  assert.ok(rb.text.includes('blocked'))

  // 16.15 click[buy now] 不被硬拦截
  const pre = await ctx.fire('tools/pre-execute', { name: 'click', arguments: { value: 'buy now' } }, async () => ({ kind: 'allow' }))
  assert.deepEqual(pre, { kind: 'allow' })

  return { dir }
}

// ── 16.12 非法 JSON 重试一次；16.13 连续失败 → manager_error ──
async function runRetryAndFail() {
  const dir = mkdtempSync(join(tmpdir(), 'mea-loop-v2-retry-'))
  const { ctx, tools, llmCalls } = makeCtx()
  apply(ctx, { stateDir: dir, plannerMode: 'llm', maxRounds: 10, manager: { provider: 'mock', model: 'mock-m' } })
  const QUERY = '买一个鼠标'
  const injected = await fireFirstPreStep(ctx, dir, QUERY, [
    textChunks('not json at all'),
    textChunks(JSON.stringify(EXECUTE_1)),
  ])
  assert.equal(llmCalls.length, 2, '非法 JSON 应重试一次')
  assert.ok(injected.messages.some(m => m.source?.plugin === 'mea-loop'))

  // 连续失败：report 后两次都失败 → manager_error
  const report = tools.find(t => t.name === 'mea_round_report')
  ctx.llm.pushScript(textChunks('bad'))
  ctx.llm.pushScript(textChunks('still bad'))
  const r = await report.execute({ summary: 'x' })
  const s = stateFile(dir)
  assert.equal(s.decision.kind, 'manager_error')
  assert.ok(r.text.includes('Manager 错误'))
}

// ── 16.16 MEA-v1 fixed 不调用 Manager LLM ──
async function runFixedNoLlm() {
  const dir = mkdtempSync(join(tmpdir(), 'mea-loop-v2-fixed-'))
  const { ctx, tools, llmCalls } = makeCtx()
  let llmTouched = false
  ctx.llm.stream = async function* () { llmTouched = true; yield* textChunks('{}') }
  apply(ctx, { stateDir: dir, plannerMode: 'fixed', maxRounds: 10 })
  await ctx.fire('agent/pre-step',
    { agent: { session: { id: 's-f' } }, messages: [userMessage('买一个鼠标')], turn: 1, step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [userMessage('买一个鼠标')] }))
  assert.equal(llmCalls.length, 0, 'fixed 模式不得调用 LLM')
  assert.equal(llmTouched, false)
  const s = stateFile(dir)
  assert.equal(s.round.number, 1)
  assert.equal(s.round.stage, 'discover')
  const report = tools.find(t => t.name === 'mea_round_report')
  const r = await report.execute({ summary: 'x' })
  assert.ok(r.text.includes('inspect'), 'fixed 第二阶段 inspect')
  assert.equal(llmCalls.length, 0)
}

// ── 无 Shopper 场景：executor_tools 不含 ask_shopper ──
async function runNoShopper() {
  const dir = mkdtempSync(join(tmpdir(), 'mea-loop-v2-noshopper-'))
  const { ctx, llmCalls } = makeCtx()
  ctx.tools.schemas = () => MOCK_EXECUTOR_TOOLS.filter(t => t.name !== 'ask_shopper')
  apply(ctx, { stateDir: dir, plannerMode: 'llm', maxRounds: 10, manager: { provider: 'mock', model: 'mock-m' } })
  await fireFirstPreStep(ctx, dir, '买一个鼠标', [textChunks(JSON.stringify(EXECUTE_1))])
  const input = JSON.parse(llmCalls[0].messages[0].content[0].text)
  assert.equal(input.runtime.available_tools.includes('ask_shopper'), false)
  assert.equal(input.runtime.shopper_available, false)
}

await runMain()
await runRetryAndFail()
await runFixedNoLlm()
await runNoShopper()

console.log('test_mea_loop_v2.mjs: all assertions passed')
