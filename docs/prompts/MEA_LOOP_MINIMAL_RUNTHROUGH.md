# 最小在线 MEA Loop 跑通任务

请在仓库：

```text
/Users/ywwl/shopping-longhorizon-harness/Shoping-Longhorizon-Harness
```

中修复并跑通第一版最小在线 MEA Loop。

先阅读：

- `docs/prompts/MEA_LOOP_PLUGIN_DESIGN.md`
- `src/mea-loop.js`
- `src/shop-tools.js`
- `harness/mea-v1/cordis.patch.yml`
- `scripts/test_mea_loop.mjs`

保留现有实现中正确的部分，不重建新架构。当前目标只是在一次 DSH 购物任务中跑通：

```text
用户初始需求
→ 初始化 Task State
→ 注入 Round 1
→ 购物 Executor 使用现有工具
→ mea_round_report
→ 更新 Task State
→ 注入 Round 2
→ mea_round_report
→ 注入 Round 3
→ 最终通过现有购物工具结束环境
```

## 严格范围

生产入口必须继续是 DSH profile + Cordis plugin。

不要新增：

- 独立 CLI MEA runner
- Python MEA Controller
- 独立 Manager LLM
- 独立 Auditor LLM
- PurchasePlan
- transaction/capability/idempotency
- trusted side-channel
- 新的购物工具
- 额外购买拦截
- 复杂需求正则解析
- 历史 trace 驱动的 State 重建

不得修改：

- `harness/h0`
- `harness/h1`
- Judge、Rubric、Report
- 历史 runs、evaluations、reports
- Shopper Simulator 的隐藏事实和系统提示

不要运行完整 benchmark，只运行测试和一个真实 DSH smoke case。

## 当前实际工具

购物工具只有：

```text
search
click
finish
ask_shopper（配置 Shopper Simulator 时）
```

MEA 可以额外注册：

```text
mea_round_report
```

不存在：

```text
select_option
open_product
buy
```

打开商品、选择规格、点击 Buy Now 都使用 `click[value]`。

## 移除额外拦截

`mea-v1` 当前阶段只验证 MEA Loop，不组合购买守卫。

修改 `harness/mea-v1/cordis.patch.yml`：

- 保留 `shop-tools`
- 保留 `mea-loop`
- 从 `mea-v1` profile 移除 `buy-guard`
- 不修改 h1 中已有的 `buy-guard`

`mea-loop` 的 `tools/pre-execute` 第一版只做观察和工具调用计数，不因为以下条件拒绝购物工具：

- `allowedTools`
- round budget
- Buy Now
- 页面类型
- 规格状态

`allowedTools` 和 `maxToolCalls` 可以保留在 Round Contract 中作为给模型看的提示，但第一版不做硬拦截。

## Task State 使用原始信息

不要依赖正则把完整用户需求拆成 category、color、brand、size 等字段。

最小 State 使用：

```json
{
  "schema": "shopping-mea-state-v1",
  "task": {
    "runId": "",
    "taskId": "",
    "envIdx": "",
    "envSession": ""
  },
  "objective": {
    "query": "用户最初的完整公开需求原文"
  },
  "clarifications": [
    {
      "round": 0,
      "question": "",
      "reply": "",
      "evidenceRef": ""
    }
  ],
  "currentObservation": {
    "toolName": "",
    "toolArguments": {},
    "modelVisibleText": ""
  },
  "rounds": [
    {
      "number": 1,
      "stage": "discover",
      "goal": "",
      "summary": "",
      "toolCalls": 0,
      "status": "running"
    }
  ],
  "round": {
    "number": 1,
    "stage": "discover",
    "goal": "",
    "toolCalls": 0,
    "status": "running"
  },
  "decision": {
    "kind": "running",
    "reason": null
  }
}
```

要求：

1. `objective.query` 原样保存当前任务的用户初始需求。
2. 每次 `ask_shopper` 返回后，把 question 和 reply 原样追加到 `clarifications`。
3. 每次购物工具返回后，只保存：
   - 实时 tool name
   - 实时 tool arguments
   - 返回给模型的同一份 `result.value.text`
4. 不读取：
   - `result.value.state`
   - `result.value.raw`
   - presentationMeta
   - reward
   - goal/gold answer
   - purchase_success
   - 历史 `model_trace.json`
   - 历史 `raw_trace.json`
   - rubric/judgment/report
5. `parseBudget()` 等已有正则函数可以暂时保留，但不得作为第一版 round 推进的必要条件。

## 固定的最小 Round 流程

第一版不追求智能规划，只需要证明 MEA 循环真实存在。

使用简单阶段：

```text
Round 1 / discover
目标：根据用户原始需求搜索候选商品。

Round 2 / inspect
目标：打开一个候选，查看商品、规格和价格信息。

Round 3 / resolve
目标：根据用户需求和当前证据继续核验；必要时使用 ask_shopper。

Round 4+ / act
目标：根据当前 Task State 继续搜索、检查、澄清或购买。
```

Round 4 以后，如果环境没有结束，可以继续创建 `act` round，直到：

- 工具返回模型可见的 `Episode finished`；或者
- 达到 `maxRounds`；或者
- 模型通过 `finish` 主动结束。

不要因为以下情况就在第一轮后把整个任务标记成 `blocked`：

- 当前页面返回了搜索首页；
- 当前页面没有规格按钮；
- 某个事实没有被正则解析；
- 当前 round report 证据不足。

## 修复第一轮注入

当前真实 smoke 中，Round 1 控制块在第一次 `search` 之后才进入模型上下文。

必须修复为：

```text
第一次购物 LLM 请求发出前
→ 已从当前用户消息初始化 State
→ 已创建 Round 1
→ 已注入 Round 1 Contract
```

检查当前 DSH 的真实 `agent/pre-step` payload 和 session seed 行为。必要时在第一次 `agent/pre-step` 中从当次 `messages` 找到真正的用户初始消息并初始化 State。

这仍然是读取当前在线消息，不是读取历史 trace。

不要只依赖可能不会为 session seed 触发的 `session/event`。

## Round Contract 内容

每轮注入模型的控制块至少包含：

```text
MEA 当前状态：

用户最初需求：
<完整 objective.query>

用户后续回复：
- <clarifications 中的原始 reply>
- 没有则写“无”

上一轮结果：
<上一轮 mea_round_report.summary>

当前可观察状态：
- 上一次工具：<toolName>
- 上一次工具参数：<arguments>
- 必要时给出当前可见页面的简短截断或摘要

当前 Round：
- 编号
- 阶段
- 目标
- 当前工具调用次数
- 建议工具

完成当前目标后调用 mea_round_report。
mea_round_report 返回下一轮目标时必须继续执行。
只有 Episode finished、finish 或 MEA 明确结束时才能输出 final。
```

Round 1 必须包含用户完整原始需求。

后续每轮必须包含所有 `ask_shopper` 原始回复，不能只保存预算正则结果。

## 修复 `mea_round_report`

当前 `mea_round_report` 只返回：

```text
MEA round acknowledged
```

这会让模型以为当前任务结束。

修改为：

1. 保存当前 round summary。
2. 将当前 round 标记为 complete。
3. 根据固定阶段创建下一轮。
4. 更新并持久化 Task State。
5. 返回完整的下一轮控制块。

例如：

```text
Round 1 已记录。

现在继续执行 Round 2。

用户最初需求：
我想买一个红色手柄的水暖排气阀，价格30元左右。

上一轮结果：
找到了候选商品A，具体规格价格尚未确认。

Round 2 目标：
打开一个候选商品，查看规格和价格。

请继续使用现有购物工具。完成后再次调用 mea_round_report。
```

只要 `decision.kind === "running"`，模型在调用 `mea_round_report` 后就应继续执行下一轮，不能输出最终回答。

同步修改 `harness/mea-v1/cordis.patch.yml` 中的 Executor System Prompt，明确：

```text
mea_round_report 结束的是当前 round。
如果工具返回下一轮目标，立即继续执行下一轮。
不得在第一次 mea_round_report 后输出 final。
```

## 同一个环境持续执行

所有 round 必须在以下同一个运行上下文中执行：

- 同一个 DSH 进程
- 同一个 DSH session
- 同一个 `SHOPSIM_ENV_IDX`
- 同一个 ShopSimulator 环境状态

不得通过以下方式伪造下一轮：

- 重启 CLI
- 重新 reset 环境
- 重新启动一个任务
- 回放历史 trace

## 简化测试

修复 `scripts/test_mea_loop.mjs` 的 fake waterfall，保证每个 handler 每次只执行一次。

测试至少覆盖：

1. 初始用户需求原样进入 State。
2. Round 1 在第一次模型请求前注入。
3. 第一次 `mea_round_report` 后生成 Round 2。
4. 第二次 `mea_round_report` 后生成 Round 3。
5. `ask_shopper` question/reply 原样进入 `clarifications`。
6. 工具事件只读取 `result.value.text`。
7. `result.value.state/raw` 中放入诱导性 reward/gold 信息，State 中不得出现。
8. 普通 `click[buy now]` 不被 mea-loop 额外拦截。
9. `Episode finished` 后 State 结束。
10. h0/h1 配置未改变。

不要把没有实际触发输入的断言命名为 Case E/I 已通过。

## 真实 DSH smoke

完成 fixture 后，安装或刷新 `mea-v1` profile，并确认：

```text
dsh --profile mea-v1 --dump-config
```

包含：

- shop-tools
- mea-loop
- 正确的 system-prompt

且不包含 mea-v1 专属的 buy-guard 注册。

运行一个真实 Shopping case。优先使用：

```text
我想买一个红色手柄的水暖排气阀，价格30元左右。
```

验收时必须从真实 DSH session 和 MEA 产物证明：

1. Round 1 控制块出现在第一次购物工具调用之前。
2. 同一个任务至少出现 Round 1、Round 2、Round 3。
3. 至少成功调用两次 `mea_round_report`。
4. 第二轮能够读取第一轮 summary。
5. 如果调用 `ask_shopper`，下一轮能看到用户原始回复。
6. 所有轮次使用同一个 `SHOPSIM_ENV_IDX`。
7. 没有读取 `raw`、presentationMeta、reward 或历史 trace。
8. 没有因为第一次 round report 而直接输出 final。
9. 最终是否购买成功可以继续由离线 evaluate 判断；本任务只验证 MEA Loop 已跑通。

如果真实 smoke 没有进入第三轮，不要运行完整 benchmark，继续修复最小循环。

## 最终交付

报告：

- 修改了哪些文件；
- Round 1 如何在第一次模型请求前注入；
- `mea_round_report` 如何创建并返回下一轮；
- Task State 如何保存初始 Query 和 `ask_shopper` 原始回复；
- 使用了哪些真实 DSH hook；
- fixture 测试结果；
- `dump-config` 结果；
- 真实 smoke 的 round 序列；
- 两次以上 `mea_round_report` 的证据；
- 是否始终使用同一个环境 session；
- 当前仍然只是 MEA control/state skeleton 的部分。

不要扩展到购买安全、复杂 Auditor 或其他 Harness 能力。
