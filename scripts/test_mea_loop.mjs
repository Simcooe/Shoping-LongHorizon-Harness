// mea-loop DSH plugin fixture 测试（第一版最小在线 loop）。
//
// 从 DSH plugin 调用路径开始：ctx.on('session/event') / 'agent/pre-step' /
// 'tools/pre-execute' / 'tools/post-execute' / ctx.tools.register。
// fake waterfall 保证每个 handler 每次 fire 只执行一次。
//
// 运行：node scripts/test_mea_loop.mjs
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  apply,
  initializeFromTask,
  buildRoundContract,
  stageForRound,
  goalForStage,
  extractReply,
} from '../src/mea-loop.js'

// ── 最小 fake DSH：只实现 mea-loop 用到的接口 ──
// waterfall：从最后一个 handler 往前逐个执行，每个 handler 每次 fire 只执行一次。
function makeCtx() {
  const handlers = new Map()
  const tools = []
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
    tools: { register(t) { tools.push(t) } },
  }
  return { ctx, handlers, tools }
}

function userMessage(text) {
  return { source: { kind: 'user' }, content: [{ type: 'text', text }] }
}

function stateFile(dir) {
  return JSON.parse(readFileSync(join(dir, 'state.json'), 'utf8'))
}

// ── 1. 初始用户需求原样进入 State ──
{
  const s = initializeFromTask({ runId: 'r', taskId: 't', envIdx: 'e', envSession: 's', query: '我想买一个红色手柄的水暖排气阀，价格30元左右。' })
  assert.equal(s.objective.query, '我想买一个红色手柄的水暖排气阀，价格30元左右。')
  assert.equal(s.round.number, 1)
  assert.equal(s.round.stage, 'discover')
}

// ── 2+3+4. 注入顺序、report 生成 Round2/3 ──
async function runCoreFlow() {
  const dir = mkdtempSync(join(tmpdir(), 'mea-loop-'))
  const { ctx, tools } = makeCtx()
  apply(ctx, { stateDir: dir, shopperUrl: 'http://x', maxRounds: 10 })

  const QUERY = '我想买一个红色手柄的水暖排气阀，价格30元左右。'
  const session = { id: 'session-core' }

  // 第一次模型请求（pre-step）：初始化 + 注入 Round 1 contract
  const injected1 = await ctx.fire('agent/pre-step',
    { agent: { session }, messages: [userMessage(QUERY)], turn: 1, step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [userMessage(QUERY)] }))

  assert.equal(injected1.kind, 'enter')
  const contract1 = injected1.messages.find(m => m.source?.plugin === 'mea-loop')
  assert.ok(contract1, 'Round 1 contract 应在第一次模型请求前注入')
  assert.ok(contract1.content[0].text.includes('Round'), 'contract 含 Round 信息')
  assert.ok(contract1.content[0].text.includes('discover'))
  assert.ok(contract1.content[0].text.includes(QUERY), 'Round 1 必须包含完整原始需求')

  let s = stateFile(dir)
  assert.equal(s.objective.query, QUERY)
  assert.equal(s.round.number, 1)
  assert.equal(s.round.status, 'running')

  // 购物工具调用：只计数，不拦截（search）
  const pre1 = await ctx.fire('tools/pre-execute',
    { name: 'search', arguments: { keywords: '水暖排气阀' } },
    async () => ({ kind: 'allow' }))
  assert.deepEqual(pre1, { kind: 'allow' }, 'search 不应被拦截')

  // 工具结果：result.value.state/raw 中放诱导性 reward/gold，State 不得出现
  await ctx.fire('tools/post-execute',
    { name: 'search', arguments: { keywords: '水暖排气阀' } },
    {
      isError: false,
      value: {
        text: '搜索结果……\n可点击的按钮: ["123456789012"]',
        state: { reward: 1, gold_asin: '999999999999', goal_options: ['秘密'] },
        raw: { reward: 1, gold: '999999999999', instruction_full: '隐藏' },
      },
    },
    async () => ({ kind: 'accept' }))

  s = stateFile(dir)
  assert.equal(s.currentObservation.toolName, 'search')
  assert.equal(s.currentObservation.modelVisibleText.includes('搜索结果'), true)
  const stateStr = JSON.stringify(s)
  assert.ok(!stateStr.includes('999999999999'), 'gold ASIN 不得进入 State')
  assert.ok(!stateStr.includes('reward'), 'reward 不得进入 State')
  assert.ok(!stateStr.includes('instruction_full'), 'instruction_full 不得进入 State')
  assert.ok(!stateStr.includes('goal_options'), 'goal_options 不得进入 State')

  // 第一次 mea_round_report → 生成 Round 2
  const report = tools.find(t => t.name === 'mea_round_report')
  assert.ok(report, 'mea_round_report 已注册')
  const r1 = await report.execute({ summary: '找到候选 A，规格价格待确认。' })
  assert.ok(r1.text.includes('Round 2'), '第一次 report 后返回 Round 2')
  assert.ok(r1.text.includes('inspect'))

  s = stateFile(dir)
  assert.equal(s.round.number, 2)
  assert.equal(s.round.stage, 'inspect')
  assert.equal(s.rounds.length, 2)
  assert.equal(s.rounds[0].summary, '找到候选 A，规格价格待确认。')
  assert.equal(s.rounds[0].status, 'complete')

  // Round 2 中 click[buy now] 不被 mea-loop 拦截
  const pre2 = await ctx.fire('tools/pre-execute',
    { name: 'click', arguments: { value: 'buy now' } },
    async () => ({ kind: 'allow' }))
  assert.deepEqual(pre2, { kind: 'allow' }, 'click[buy now] 不应被 mea-loop 拦截')

  // 第二次 mea_round_report → 生成 Round 3
  const r2 = await report.execute({ summary: '打开候选 A，规格价格已确认。' })
  assert.ok(r2.text.includes('Round 3'), '第二次 report 后返回 Round 3')
  assert.ok(r2.text.includes('resolve'))

  s = stateFile(dir)
  assert.equal(s.round.number, 3)
  assert.equal(s.round.stage, 'resolve')
  assert.equal(s.rounds.length, 3)
  assert.equal(s.rounds[1].summary, '打开候选 A，规格价格已确认。')

  // 第二轮能读取第一轮 summary（Round 2 contract 含上一轮结果）
  assert.ok(r1.text.includes('找到候选 A，规格价格待确认。'), 'Round 2 contract 应含第一轮 summary')

  // ask_shopper → question/reply 原样进入 clarifications
  await ctx.fire('tools/post-execute',
    { name: 'ask_shopper', arguments: { question: '预算能放宽到 40 元吗？' } },
    { isError: false, value: { text: '用户回复：可以，最多 40 元。', state: {}, raw: {} } },
    async () => ({ kind: 'accept' }))

  s = stateFile(dir)
  assert.equal(s.clarifications.length, 1)
  assert.equal(s.clarifications[0].question, '预算能放宽到 40 元吗？')
  assert.equal(s.clarifications[0].reply, '可以，最多 40 元。')
  assert.equal(s.clarifications[0].round, 3)

  // 后续 round contract 必须包含所有 ask_shopper 原始回复
  const r3 = await report.execute({ summary: '澄清了预算。' })
  assert.ok(r3.text.includes('可以，最多 40 元。'), '下一轮 contract 应含原始用户回复')

  return { dir, report }
}

// ── 9. Episode finished 后 State 结束 ──
async function runTerminal() {
  const dir = mkdtempSync(join(tmpdir(), 'mea-loop-term-'))
  const { ctx } = makeCtx()
  apply(ctx, { stateDir: dir, shopperUrl: 'http://x', maxRounds: 10 })
  await ctx.fire('agent/pre-step',
    { agent: { session: { id: 's-term' } }, messages: [userMessage('买一个鼠标')], turn: 1, step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [userMessage('买一个鼠标')] }))
  await ctx.fire('tools/post-execute',
    { name: 'click', arguments: { value: 'buy now' } },
    { isError: false, value: { text: 'Episode finished.\n\n搜索功能是否可用: False\n\n可点击的按钮: []' } },
    async () => ({ kind: 'accept' }))
  const s = stateFile(dir)
  assert.equal(s.decision.kind, 'terminal')
  assert.equal(s.round.status, 'complete')
}

// ── 纯函数：固定阶段 ──
{
  assert.equal(stageForRound(1), 'discover')
  assert.equal(stageForRound(2), 'inspect')
  assert.equal(stageForRound(3), 'resolve')
  assert.equal(stageForRound(4), 'act')
  assert.equal(stageForRound(9), 'act')
  assert.ok(goalForStage('discover').length > 0)
  assert.equal(extractReply('用户回复：可以，最多 40 元。'), '可以，最多 40 元。')
}

await runCoreFlow()
await runTerminal()

console.log('test_mea_loop.mjs: all assertions passed')
