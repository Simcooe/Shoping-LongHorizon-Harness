// mea-loop v3 Auditor LLM fixture/mock 测试。
//
// 通过真实 ctx.llm.stream 调用形状验证 Auditor + Manager 行为，不发起真实模型请求。
// mock 记录每次调用的 options，区分 Manager 与 Auditor（按 system prompt 关键词）。
//
// 运行：node scripts/test_mea_loop_v3.mjs
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { apply, parseAuditorOutput } from '../src/mea-loop.js'

function textChunks(text) {
  return [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'finish', reason: { kind: 'stop' } },
  ]
}

const MANAGER_EXECUTE = {
  decision: 'execute',
  state_summary: '继续核验候选规格',
  open_gaps: [],
  reason: 'r',
  next_round: {
    goal: '核验候选规格',
    suggested_tools: ['click'],
    max_tool_calls: 4,
    completion_criteria: ['看到规格与价格'],
  },
}

const AUDIT_COMPLETE = {
  round_status: 'complete',
  verified_summary: '已选择规格并看到价格',
  supported_claims: [{ claim: '已选择规格', evidence_refs: ['ev-1'] }],
  unsupported_claims: [],
  open_gaps: [],
  integrity: 'clean',
}

const AUDIT_INCOMPLETE = {
  round_status: 'incomplete',
  verified_summary: '当前只选择了1支装',
  supported_claims: [{ claim: '选择了1支装', evidence_refs: ['ev-1'] }],
  unsupported_claims: [{ claim: '已满足100根', reason: '只有1支装证据，无数量操作' }],
  open_gaps: ['需要100支装规格'],
  integrity: 'suspect',
}

const MOCK_EXECUTOR_TOOLS = [
  { name: 'search', description: 'Search the shop for products.', parameters: { type: 'object', properties: { keywords: { type: 'string' } } } },
  { name: 'click', description: 'Click a value on the current page.', parameters: { type: 'object', properties: { value: { type: 'string' } } } },
  { name: 'finish', description: 'End the episode without purchasing (this is not a success).', parameters: { type: 'object', properties: { reason: { enum: ['no_suitable_product'] } } } },
  { name: 'ask_shopper', description: 'Ask the user one clarifying question and get their reply.', parameters: { type: 'object', properties: { question: { type: 'string' } } } },
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
        if (typeof script === 'function') yield* script(options)
        else yield* script
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
function isManagerCall(opts) {
  return String(opts.system).includes('Manager')
}
function isAuditorCall(opts) {
  return String(opts.system).includes('Auditor')
}

// ── parseAuditorOutput 单测 ──
{
  const p = parseAuditorOutput(JSON.stringify(AUDIT_COMPLETE), ['ev-1'])
  assert.equal(p.roundStatus, 'complete')
  // 未知 evidence ref 拒绝
  assert.throws(() => parseAuditorOutput(JSON.stringify(AUDIT_COMPLETE), []), /unknown evidence ref/)
  assert.throws(() => parseAuditorOutput(JSON.stringify(AUDIT_COMPLETE), ['ev-9']), /unknown evidence ref/)
  // 未知顶层字段拒绝
  assert.throws(() => parseAuditorOutput(JSON.stringify({ ...AUDIT_COMPLETE, next_round: {} }), ['ev-1']), /unknown top-level/)
  // round_status 非法拒绝
  assert.throws(() => parseAuditorOutput(JSON.stringify({ ...AUDIT_COMPLETE, round_status: 'success' }), ['ev-1']), /invalid round_status/)
  // 存在 unsupported claims 时 integrity 不能是 clean
  assert.throws(() => parseAuditorOutput(JSON.stringify({
    ...AUDIT_COMPLETE,
    unsupported_claims: [{ claim: '无证据声明', reason: '当前轮没有公开证据支持' }],
  }), ['ev-1']), /integrity cannot be clean/)
  // code fence
  const fenced = parseAuditorOutput('```json\n' + JSON.stringify(AUDIT_COMPLETE) + '\n```', ['ev-1'])
  assert.equal(fenced.roundStatus, 'complete')
}

async function runMain() {
  const dir = mkdtempSync(join(tmpdir(), 'mea-loop-v3-'))
  const { ctx, tools, llmCalls } = makeCtx()
  apply(ctx, { stateDir: dir, plannerMode: 'llm', auditorMode: 'llm', maxRounds: 10, manager: { provider: 'mock', model: 'mock-m' }, auditor: { provider: 'mock', model: 'mock-a' } })

  const QUERY = '我想买一个红色手柄的水暖排气阀，价格30元左右。'
  // 任务开始：先 Manager（生成 Round 1）
  ctx.llm.pushScript(textChunks(JSON.stringify(MANAGER_EXECUTE)))
  await ctx.fire('agent/pre-step',
    { agent: { session: { id: 'session-v3' } }, messages: [userMessage(QUERY)], turn: 1, step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [userMessage(QUERY)] }))

  assert.equal(llmCalls.length, 1, '任务开始先调用 Manager')
  assert.equal(isManagerCall(llmCalls[0]), true)
  assert.equal(llmCalls[0].tools, undefined)

  let s = stateFile(dir)
  assert.equal(s.round.number, 1)
  assert.equal(s.manager.calls, 1)
  assert.equal(s.auditor.calls, 0)

  // 一轮工具事件
  await ctx.fire('tools/pre-execute', { name: 'click', arguments: { value: 'buy now' } }, async () => ({ kind: 'allow' }))
  await ctx.fire('tools/post-execute',
    { name: 'click', arguments: { value: '123456789012' } },
    { isError: false, value: { text: '价格: 30\n可点击的按钮: ["buy now"]', state: { reward: 1, gold: '999' }, raw: { gold: '999' } } },
    async () => ({ kind: 'accept' }))

  // mea_round_report → 先 Auditor，再 Manager
  const report = tools.find(t => t.name === 'mea_round_report')
  ctx.llm.pushScript(textChunks(JSON.stringify(AUDIT_COMPLETE)))
  ctx.llm.pushScript(textChunks(JSON.stringify(MANAGER_EXECUTE)))
  const r = await report.execute({ summary: '已选择规格，价格30元' })

  assert.equal(llmCalls.length, 3, 'report 后先 Auditor 再 Manager')
  assert.equal(isAuditorCall(llmCalls[1]), true, '第二次调用应是 Auditor')
  assert.equal(isManagerCall(llmCalls[2]), true, '第三次调用应是 Manager')

  // Auditor 输入
  const auditInput = JSON.parse(llmCalls[1].messages[0].content[0].text)
  assert.equal(auditInput.round_contract.number, 1)
  assert.equal(auditInput.executor_report.summary, '已选择规格，价格30元')
  assert.equal(auditInput.tool_events.length, 1)
  assert.equal(auditInput.tool_events[0].evidence_id, 'ev-1')
  assert.ok(auditInput.executor_tools.some(t => t.name === 'click'))
  const auditStr = JSON.stringify(llmCalls[1])
  assert.ok(!auditStr.includes('"reward"'))
  assert.ok(!auditStr.includes('"gold"'))
  assert.ok(!auditStr.includes('"raw"'))

  // Manager 输入：含 audit，不含原始 executor report
  const mgrInput = JSON.parse(llmCalls[2].messages[0].content[0].text)
  assert.ok(!JSON.stringify(mgrInput).includes('executor_report'), 'v3 Manager 输入不得含 executor_report')
  assert.equal(mgrInput.last_round.audit.round_status, 'complete')
  assert.equal(mgrInput.last_round.audit.verified_summary, '已选择规格并看到价格')

  s = stateFile(dir)
  assert.equal(s.rounds[0].audit.roundStatus, 'complete')
  assert.equal(s.auditor.calls, 1)

  return { dir, ctx, tools, llmCalls }
}

// Auditor 失败重试一次；连续失败 → auditor_error 且不调用 Manager
async function runAuditorFail() {
  const dir = mkdtempSync(join(tmpdir(), 'mea-loop-v3-fail-'))
  const { ctx, tools, llmCalls } = makeCtx()
  apply(ctx, { stateDir: dir, plannerMode: 'llm', auditorMode: 'llm', maxRounds: 10, manager: { provider: 'mock', model: 'mock-m' }, auditor: { provider: 'mock', model: 'mock-a' } })

  ctx.llm.pushScript(textChunks(JSON.stringify(MANAGER_EXECUTE)))
  await ctx.fire('agent/pre-step',
    { agent: { session: { id: 's' } }, messages: [userMessage('买一个鼠标')], turn: 1, step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [userMessage('买一个鼠标')] }))

  const report = tools.find(t => t.name === 'mea_round_report')
  ctx.llm.pushScript(textChunks('bad json'))
  ctx.llm.pushScript(textChunks('still bad'))
  const r = await report.execute({ summary: 'x' })

  // 1 Manager + 2 Auditor 失败重试 = 3 次
  assert.equal(llmCalls.length, 3)
  assert.equal(isAuditorCall(llmCalls[1]), true)
  assert.equal(isAuditorCall(llmCalls[2]), true)
  const s = stateFile(dir)
  assert.equal(s.decision.kind, 'auditor_error')
  assert.ok(r.text.includes('Auditor 错误'))
}

// v2 调用 Manager 但不调用 Auditor
async function runV2NoAuditor() {
  const dir = mkdtempSync(join(tmpdir(), 'mea-loop-v3-v2-'))
  const { ctx, tools, llmCalls } = makeCtx()
  apply(ctx, { stateDir: dir, plannerMode: 'llm', auditorMode: 'none', maxRounds: 10, manager: { provider: 'mock', model: 'mock-m' } })
  ctx.llm.pushScript(textChunks(JSON.stringify(MANAGER_EXECUTE)))
  await ctx.fire('agent/pre-step',
    { agent: { session: { id: 's' } }, messages: [userMessage('买一个鼠标')], turn: 1, step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [userMessage('买一个鼠标')] }))
  const report = tools.find(t => t.name === 'mea_round_report')
  ctx.llm.pushScript(textChunks(JSON.stringify(MANAGER_EXECUTE)))
  await report.execute({ summary: 'x' })
  assert.equal(llmCalls.length, 2)
  assert.equal(llmCalls.some(c => isAuditorCall(c)), false, 'v2 不得调用 Auditor')
}

await runMain()
await runAuditorFail()
await runV2NoAuditor()

console.log('test_mea_loop_v3.mjs: all assertions passed')
