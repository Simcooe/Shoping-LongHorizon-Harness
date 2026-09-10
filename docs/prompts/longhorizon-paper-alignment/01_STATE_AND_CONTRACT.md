# 阶段1：结构化 Task State、Contract 与 Audit Reducer

## 任务

为新 profile `mea-v4-paper` 实现论文式持久状态模型。此阶段只完成纯数据结构、
校验器与 reducer，不接真实模型，不改变现有 mea-v3 runtime。

## 开始前必读

- `docs/prompts/longhorizon-paper-alignment/README.md`
- `reference/LongHorizon-Harness.pdf` 的 Method 2.1、2.2、2.4
- `src/mea-loop.js` 中现有 state、Manager/Auditor schema

## 新增建议

```text
src/mea-v4/state.js
src/mea-v4/schema.js
scripts/test_mea_v4_state.mjs
harness/mea-v4-paper/package.json
harness/mea-v4-paper/cordis.patch.yml
```

## Task State 最小 schema

```json
{
  "schema": "longhorizon-task-state-v1",
  "task": {"id": "...", "original_goal": "..."},
  "requirements": [
    {
      "id": "req-...",
      "text": "...",
      "source": "initial_request | shopper_reply",
      "status": "pending | completed | blocked | untrusted",
      "evidence_refs": ["audit-.../evidence-..."]
    }
  ],
  "artifacts": [],
  "facts": [],
  "audit_history": [],
  "round": 0,
  "decision": {"kind": "execute | done | blocked | ask", "reason": "..."}
}
```

购物场景的 selected product、selected options、公开价格、包装数量和购买授权可建模为
fact/artifact，但不要读入隐藏 TaskFacts 或 reward。

## Contract 最小 schema

```json
{
  "id": "contract-...",
  "goal": "一个可验证子目标",
  "acceptance_criteria": ["..."],
  "boundary_constraints": ["不得购买", "不得改变已选规格"],
  "dependencies": ["req-...", "fact-..."],
  "relevant_state_ids": ["req-...", "fact-..."],
  "relevant_audit_ids": ["audit-..."],
  "allowed_tools": ["search", "click", "ask_shopper", "finish"],
  "max_tool_calls": 5,
  "timeout_seconds": 1800
}
```

## Reducer 硬规则

- Executor report 不能直接更新 requirement/artifact/fact。
- `completed` 必须引用 status=`complete`、integrity=`clean` 的 Audit Report。
- evidence ref 必须存在并属于对应 audit。
- suspect/violation audit 只能产生 pending/blocked/untrusted 更新。
- 新 shopper 回复可增加或修改 requirement，但必须保存原文与来源。
- done 只有在所有有效 requirement 均 completed 且没有 unresolved integrity violation
  时合法。

## 测试

- 初始要求全部 pending。
- 无 audit 的 Executor claim 不能变 completed。
- clean audit 可以推进状态。
- suspect/violation 不能推进 completed。
- 不存在或跨 audit evidence ref 被拒绝。
- 原始目标始终保留。
- ask/reply 更新需求而不读取私有事实。
- JSON round-trip 后状态一致。

## 完成条件

新纯函数测试全部通过；现有 `test_mea_loop*.mjs` 全部通过；没有接入真实运行。

