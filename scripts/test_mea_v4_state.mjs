// mea-v4 阶段1 — 结构化 Task State、Contract 与 Audit Reducer 测试。
//
// 只测试纯函数/校验器/reducer，不接入真实模型，不改变 mea-v3 runtime。
//
// 运行：node scripts/test_mea_v4_state.mjs
import assert from 'node:assert/strict'
import {
  TASK_STATE_SCHEMA,
  createTaskState,
  addRequirement,
  applyAuditReport,
  applyDecision,
  finalize,
  cloneState,
} from '../src/mea-v4/state.js'
import {
  validateContract,
  validateAuditReport,
  makeEvidence,
} from '../src/mea-v4/schema.js'

const GOAL = '我想买一个红色手柄的水暖排气阀，价格30元左右。'

function cleanAudit(overrides = {}) {
  return {
    id: 'audit-1',
    round: 1,
    contract_id: 'contract-1',
    status: 'complete',
    integrity: 'clean',
    verified_summary: '已选择规格并看到价格',
    evidence: [
      makeEvidence({ id: 'ev-1', kind: 'read_only_observation', summary: '价格页显示 30 元' }),
    ],
    new_facts: [],
    status_updates: [],
    open_gaps: [],
    ...overrides,
  }
}

function suspectAudit(overrides = {}) {
  return cleanAudit({ id: 'audit-2', status: 'incomplete', integrity: 'suspect', ...overrides })
}

function violationAudit(overrides = {}) {
  return cleanAudit({ id: 'audit-3', status: 'blocked', integrity: 'violation', ...overrides })
}

// ── 1. 初始要求全部 pending，原始目标保留 ──
{
  const s = createTaskState({
    taskId: 'task-1',
    originalGoal: GOAL,
    initialRequirements: [
      { text: '红色手柄', source: 'initial_request' },
      { text: '价格30元左右', source: 'initial_request' },
    ],
  })
  assert.equal(s.schema, TASK_STATE_SCHEMA)
  assert.equal(s.task.original_goal, GOAL)
  assert.equal(s.requirements.length, 2)
  assert.ok(s.requirements.every(r => r.status === 'pending'))
  assert.equal(s.requirements[0].id, 'req-1')
  assert.equal(s.requirements[1].id, 'req-2')
}

// ── 2. 无 audit 的 Executor claim 不能变 completed ──
// reducer 不提供任何接受 executor report 的入口；直接改状态不被允许。
{
  const s = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }] })
  assert.throws(() => applyDecision(s, { kind: 'execute', reason: 'x', statusUpdates: [{ id: 'req-1', status: 'completed', evidence_refs: [] }] }), /unknown decision field/)
}

// ── 3. clean audit 可以推进状态 ──
{
  const s0 = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }] })
  const audit = cleanAudit({
    status_updates: [{ id: 'req-1', status: 'completed', evidence_refs: ['ev-1'] }],
  })
  const s1 = applyAuditReport(s0, audit)
  assert.equal(s1.requirements[0].status, 'completed')
  assert.deepEqual(s1.requirements[0].evidence_refs, ['ev-1'])
  assert.equal(s1.audit_history.length, 1)
  assert.equal(s1.round, 1)
  // 原始目标始终保留
  assert.equal(s1.task.original_goal, GOAL)
  // 不可变：原状态未被污染
  assert.equal(s0.requirements[0].status, 'pending')
}

// ── 4. suspect / violation 不能推进 completed ──
{
  const s0 = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }] })
  assert.throws(() => applyAuditReport(s0, suspectAudit({
    status_updates: [{ id: 'req-1', status: 'completed', evidence_refs: ['ev-1'] }],
  })), /suspect\/violation audit cannot produce completed/)
  assert.throws(() => applyAuditReport(s0, violationAudit({
    status_updates: [{ id: 'req-1', status: 'completed', evidence_refs: ['ev-1'] }],
  })), /suspect\/violation audit cannot produce completed/)
  // suspect/violation 只能落到 pending/blocked/untrusted
  const s1 = applyAuditReport(s0, suspectAudit({
    status_updates: [{ id: 'req-1', status: 'untrusted', evidence_refs: ['ev-1'] }],
  }))
  assert.equal(s1.requirements[0].status, 'untrusted')
}

// ── 5. 不存在或跨 audit evidence ref 被拒绝 ──
{
  const s0 = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }] })
  // 不存在的 ref
  assert.throws(() => applyAuditReport(s0, cleanAudit({
    status_updates: [{ id: 'req-1', status: 'completed', evidence_refs: ['ev-404'] }],
  })), /does not belong to audit/)
  // 跨 audit ref：先提交 audit-1，再用 audit-2 引用 ev-1
  const s1 = applyAuditReport(s0, cleanAudit())
  const audit2 = cleanAudit({
    id: 'audit-2',
    evidence: [makeEvidence({ id: 'ev-2', summary: '另一个证据' })],
    status_updates: [{ id: 'req-1', status: 'completed', evidence_refs: ['ev-1'] }],
  })
  assert.throws(() => applyAuditReport(s1, audit2), /does not belong to audit/)
  // 空 evidence refs
  assert.throws(() => applyAuditReport(s0, cleanAudit({
    status_updates: [{ id: 'req-1', status: 'completed', evidence_refs: [] }],
  })), /completed requires evidence_refs/)
}

// ── 6. 原始目标始终保留 ──
{
  const s0 = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }] })
  const s1 = applyAuditReport(s0, cleanAudit({
    status_updates: [{ id: 'req-1', status: 'completed', evidence_refs: ['ev-1'] }],
  }))
  const s2 = addRequirement(s1, { text: '全铜材质', source: 'shopper_reply' })
  const s3 = applyDecision(s2, { kind: 'done', reason: 'all done' })
  assert.equal(s3.task.original_goal, GOAL)
  assert.equal(s3.task.id, 't')
}

// ── 7. ask/reply 更新需求而不读取私有事实 ──
{
  const s0 = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }] })
  const s1 = addRequirement(s0, { text: '全铜材质，6分接口', source: 'shopper_reply' })
  assert.equal(s1.requirements.length, 2)
  const added = s1.requirements[1]
  assert.equal(added.source, 'shopper_reply')
  assert.equal(added.text, '全铜材质，6分接口')
  assert.equal(added.status, 'pending')
  // 修改已有 requirement 保留原文
  const s2 = addRequirement(s1, { id: 'req-1', text: '红色手柄（已确认）', source: 'shopper_reply' })
  assert.equal(s2.requirements[0].text, '红色手柄（已确认）')
  assert.equal(s2.requirements[0].source, 'shopper_reply')
  assert.equal(s2.requirements.length, 2)
  // 状态串里不含任何私有键
  const serialized = JSON.stringify(s2)
  for (const key of ['reward', 'gold', 'task_facts', 'gold_asin', 'goal_options']) {
    assert.ok(!serialized.includes(key), `state 不得包含私有键 ${key}`)
  }
}

// ── 8. JSON round-trip 后状态一致 ──
{
  const s0 = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }, { text: '30元' }] })
  const s1 = applyAuditReport(s0, cleanAudit({
    status_updates: [{ id: 'req-1', status: 'completed', evidence_refs: ['ev-1'] }],
  }))
  const s2 = applyDecision(s1, { kind: 'execute', reason: 'continue' })
  const roundTripped = JSON.parse(JSON.stringify(s2))
  assert.deepEqual(roundTripped, s2)
}

// ── 9. done 合法性：必须全部 completed 且无 violation ──
{
  const s0 = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }, { text: '30元' }] })
  // 尚未 completed
  assert.equal(finalize(s0).ok, false)
  const s1 = applyAuditReport(s0, cleanAudit({
    status_updates: [
      { id: 'req-1', status: 'completed', evidence_refs: ['ev-1'] },
      { id: 'req-2', status: 'completed', evidence_refs: ['ev-1'] },
    ],
  }))
  assert.equal(finalize(s1).ok, true)
  const s2 = applyDecision(s1, { kind: 'done', reason: 'all done' })
  assert.equal(s2.decision.kind, 'done')
  // 存在 unresolved violation 时不可 done
  const s3 = applyAuditReport(s1, violationAudit({
    id: 'audit-9',
    evidence: [makeEvidence({ id: 'ev-9', summary: '检测到购买后修改' })],
  }))
  assert.equal(finalize(s3).ok, false)
  assert.match(finalize(s3).reason, /violation/)
}

// ── 10. Contract 校验 ──
{
  const c = validateContract({
    id: 'contract-1',
    goal: '核验候选规格与价格',
    acceptance_criteria: ['看到规格', '看到价格'],
    boundary_constraints: ['不得购买'],
    dependencies: ['req-1'],
    relevant_state_ids: ['req-1'],
    relevant_audit_ids: [],
    allowed_tools: ['search', 'click'],
    max_tool_calls: 5,
    timeout_seconds: 1800,
  })
  assert.equal(c.id, 'contract-1')
  assert.deepEqual(c.allowed_tools, ['search', 'click'])
  assert.equal(c.timeout_seconds, 1800)
  // 非法工具
  assert.throws(() => validateContract({ ...c, allowed_tools: ['buy'] }), /unsupported tool/)
  // 缺失 acceptance_criteria
  assert.throws(() => validateContract({ ...c, acceptance_criteria: [] }), /non-empty/)
  // 未知字段
  assert.throws(() => validateContract({ ...c, extra: 1 }), /unknown contract field/)
  // 超时范围
  assert.throws(() => validateContract({ ...c, timeout_seconds: 0 }), /timeout_seconds/)
  assert.throws(() => validateContract({ ...c, max_tool_calls: 0 }), /max_tool_calls/)
}

// ── 11. Audit Report 校验 ──
{
  const a = validateAuditReport(cleanAudit())
  assert.equal(a.status, 'complete')
  assert.equal(a.integrity, 'clean')
  assert.equal(a.evidence[0].id, 'ev-1')
  // 非法 status
  assert.throws(() => validateAuditReport(cleanAudit({ status: 'success' })), /invalid audit status/)
  // 非法 integrity
  assert.throws(() => validateAuditReport(cleanAudit({ integrity: 'ok' })), /invalid audit integrity/)
  // evidence kind 必须只读
  assert.throws(() => validateAuditReport(cleanAudit({
    evidence: [makeEvidence({ id: 'ev-1', kind: 'click', summary: 'x' })],
  })), /read-only/)
  // evidence 与 evidence_ids 不一致
  assert.throws(() => validateAuditReport(cleanAudit({
    evidence_ids: ['ev-other'],
  })), /disagree/)
  // 未知字段
  assert.throws(() => validateAuditReport(cleanAudit({ extra: 1 })), /unknown audit report field/)
}

// ── 12. duplicate audit id 拒绝 ──
{
  const s0 = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }] })
  const s1 = applyAuditReport(s0, cleanAudit())
  assert.throws(() => applyAuditReport(s1, cleanAudit()), /duplicate audit id/)
}

// ── 13. cloneState 深度拷贝 ──
{
  const s0 = createTaskState({ taskId: 't', originalGoal: GOAL, initialRequirements: [{ text: '红色手柄' }] })
  const s1 = cloneState(s0)
  s1.requirements[0].status = 'completed'
  assert.equal(s0.requirements[0].status, 'pending')
}

console.log('test_mea_v4_state.mjs: all assertions passed')
