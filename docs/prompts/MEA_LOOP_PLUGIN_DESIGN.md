# `mea-loop` DSH 插件设计：数据流、状态机与具体 Shop Case

> 给另一个会话执行。先设计清楚再写代码。
>
> 目标：在现有 Shopping Harness 上，使用 DSH plugin 实现一个最小 MEA loop。参考论文的思想，不照抄论文名词，不创建独立 CLI，不创建独立 Python MEA 服务，不把评测脚本当运行时。

> 本文设计的是**在线运行时 MEA**。它不读取已有的 `model_trace.json`、`raw_trace.json`、rubric、judgment 或 report 来生成 State。Task State 在 DSH session 开始时由用户初始需求创建，随后只根据当前 DSH 工具定义、实时工具调用、模型可见的工具返回以及 `ask_shopper` 的模型可见回复在线更新。历史 trace 只用于发现 h0 的失败类型和任务结束后的离线评测。

---

## 0. 先固定一句话

`mea-loop` 不是另一个购物工具，也不是另一个模型 Agent。它是 DSH 中的一个**运行时控制插件**：

```text
接收用户最开始的公开需求并初始化 Task State
  → 观察 DSH 中实时发生的 shop-tools 工具调用
  → 从模型可见的实时工具结果更新 Task State
  → 将 State 压缩成下一轮可用的任务记忆
  → 用有限 round 目标约束模型继续行动
  → 对本轮公开结果做确定性检查
  → 把检查结果写回 State
```

核心闭环：

```text
Task State
   ↓
本轮目标 / bounded round
   ↓
DSH 模型调用 shop-tools
   ↓
模型可见的实时观察结果
   ↓
Reducer 更新 Task State
   ↓
下一轮目标
```

MEA 的重点不是“多写几个 Agent”，而是：

> **不要让一个购物任务退化成无限 transcript；让模型在每个有限子目标上行动，下一轮依据结构化状态继续。**

这里的 State 是在线执行过程中逐步形成的外部任务记忆，不是执行结束后从 trace 重建的结果。

---

# 1. 生产架构：只通过 DSH Plugin 实现

## 1.1 生产入口

生产运行链路必须是：

```text
DSH
  → mea-v1 / MEA profile
      → shop-tools plugin
      → buy-guard plugin（若该 profile 启用）
      → mea-loop plugin
```

建议新增：

```text
src/mea-loop.js
```

并在 package exports 中暴露：

```json
{
  "./mea-loop": "./src/mea-loop.js"
}
```

在 MEA 专用 DSH profile 的 `cordis.patch.yml` 中注册。具体 `cordis` 插件 patch 语法、`ctx.on` 事件名称、工具注册和上下文注入 API 必须以当前 DSH 版本和现有 `src/buy-guard.js` / `src/shop-tools.js` 为准；不能凭想象增加不存在的 hook。

## 1.2 各插件唯一职责

```text
shop-tools
  注册 search / click / finish / ask_shopper
  调用 ShopSimulator
  生成模型可见 text
  通过 presentationMeta 保存评测/日志侧信息，但这些隐藏信息不是 MEA 运行输入

buy-guard
  继续负责已有的 Buy Now 页面前置检查
  不负责 MEA round、不负责 State、不负责 Manager

mea-loop
  从用户初始公开需求初始化 MEA Task State
  维护 MEA Task State
  管理 round 边界
  统计本轮工具调用
  读取实时 tool name / arguments 和模型可见 text
  做确定性检查
  生成下一轮摘要/目标
  在越界时拒绝工具调用
  持久化 State / round / evidence
```

不要把 MEA 逻辑复制到 `shop-tools.js`，也不要让 `mea-loop` 重新实现 ShopSimulator 协议。

当前模型可见工具只有：

```text
search
click
finish
ask_shopper（仅配置 Shopper Simulator 时注册）
```

不存在独立的 `open_product`、`select_option` 或 `buy` 工具。打开商品、选择规格和购买都通过 `click[value]` 完成。

## 1.3 第一版中的 LLM 和系统提示词边界

当前在线链路有两个实际 LLM 角色：

```text
购物 Executor LLM
  由 DSH 主 Agent 发起
  系统提示词由 MEA 专用 profile 的 system-prompt 插件配置

Shopper Simulator LLM
  由当前 Shopper Simulator 服务发起
  系统提示词保留在拥有该模型调用的 Shopper 服务中
  隐藏 persona / instruction_full 只允许该角色读取
```

不能把 Shopper Simulator 的系统提示或隐藏用户事实放进购物 Executor 的 `cordis.patch.yml`。`ask_shopper` 只向 Executor 返回本次用户回复。

第一版没有独立 Manager LLM 和 Auditor LLM。`mea-loop` 用确定性规则产生 round 控制块并核验实时公开证据。以后真正增加 Manager/Auditor 模型调用时，它们的角色提示应由 MEA 专用 plugin/profile 分别配置，并且只能接收本文规定的公开 State 和 evidence；在模型调用尚不存在之前，不能只靠多写两段 prompt 就声称实现了两个角色。

---

# 2. 先理解 DSH 中的一次工具调用

以一次 `click` 为例，数据流应该是：

```text
模型产生 tool call
  ↓
mea-loop pre-execute
  ├── 读取当前 round 状态
  ├── 检查 round 是否仍可行动
  ├── 检查工具是否属于本轮允许工具
  └── 允许 / 拒绝
  ↓
shop-tools execute
  ↓
ShopSimulator /api/shop_agent
  ↓
shop-tools 返回：
  ├── text       → 模型看到
  ├── state      → presentationMeta / 评测与日志侧，MEA v1 不消费
  └── raw        → 评测/日志侧，MEA v1 不消费
  ↓
mea-loop post-execute
  ├── 读取本次实时 tool name / arguments
  ├── 读取本次返回给模型的 text
  ├── 写 evidence
  ├── 更新 candidates / facts / current_observation
  ├── 运行本轮 acceptance checks
  ├── 判断是否 round complete
  └── 允许 DSH 继续或触发 round boundary
```

重要：

```text
模型看到用户需求、自己的 tool call 和工具返回的 text
MEA v1 从同一批实时可见信息更新 State
评测在任务结束后继续读取 raw/presentationMeta
```

MEA v1 的运行输入严格限定为：

```text
1. DSH session 开始时的用户初始公开需求
2. 当前实际注册的工具定义
3. 实时发生的 tool name / arguments
4. search / click / finish 返回给模型的 text
5. ask_shopper 返回给模型的用户回复
6. MEA 自己产生的 round id、调用计数、状态版本和检查结果
```

MEA v1 不读取已有的：

```text
runs/**/model_trace.json
runs/**/raw_trace.json
rubric
judgment
report
历史任务的 State 或 evidence
```

这些数据只用于基线问题分析和任务结束后的离线评测，不参与新任务的在线决策。

MEA v1 不得读取、推断或重新注入以下后台字段：

```text
reward
goal（环境隐藏 goal）
gold ASIN
known_valid_asins
reward_valid
purchase_success
termination_reason（如果它来自后台判定）
instruction_full
result.value.state
result.value.raw
完整 presentationMeta
```

`goal` 这个词在 MEA 中只能表示公开的**本轮子任务目标**，不能表示 ShopSimulator 隐藏目标。

如果以后为了减少文本解析而使用结构化 `observation_state`，必须先证明每一个白名单字段都与模型当时看到的 `text` 或它刚刚执行的动作语义等价，并在单独版本中实现；不属于本次 MEA v1。

---

# 3. 最小 Task State

第一版只维护一个任务状态对象，不做完整数据库，不做复杂 event sourcing。

```js
{
  schema: "shopping-mea-state-v1",

  task: {
    runId: "...",
    taskId: "...",
    envIdx: "...",
    envSession: "..."
  },

  objective: {
    query: "用户公开任务文本"
  },

  requirements: [
    {
      key: "budget",
      value: "150元左右",
      source: "initial_query | shopper_reply",
      status: "active | superseded | revoked | unknown",
      version: 0,
      sourceRef: "..."
    }
  ],

  candidates: [
    {
      asin: "...",
      title: "...",
      status: "seen | inspected | rejected | selected",
      evidenceRefs: ["ev-..."],
      lastSeenRound: 1
    }
  ],

  facts: [
    {
      key: "opened_product | clicked_option | displayed_price | inferred_page_kind",
      value: "...",
      status: "observed | verified | unknown",
      source: "initial_query | shopper_reply | tool_call | model_visible_text | deterministic_check",
      evidenceRef: "ev-...",
      round: 1
    }
  ],

  currentObservation: {
    sourceTool: "click",
    sourceArguments: {value: "..."},
    modelVisibleText: "模型本次实际看到的页面文本",
    extracted: {
      searchAvailable: true,
      clickableValues: ["..."],
      displayedProduct: null,
      displayedPrice: null
    },
    readSequence: 3
  },

  gaps: [
    {
      key: "need_candidate | need_product_evidence | need_price | need_option | need_user_clarification",
      status: "open | resolved",
      description: "公开可解释的缺口",
      round: 1
    }
  ],

  round: {
    number: 1,
    goal: "核验当前候选的商品身份和价格",
    allowedTools: ["click", "search"],
    maxToolCalls: 5,
    toolCalls: 2,
    status: "running | complete | incomplete | blocked"
  },

  history: {
    lastRoundSummary: "短摘要，不保存完整 transcript"
  },

  decision: {
    kind: "running | ask | completed | blocked | unknown",
    reason: null
  }
}
```

## 3.1 State 字段的权威来源

```text
objective.query
  在 DSH session 开始时直接来自当前用户的公开任务输入

requirements
  来自公开 Query 和 ask_shopper 回复

candidates
  只能来自实时 search/click 返回给模型的 text

facts
  只能来自初始需求、ask_shopper 回复、实时 tool call、模型可见 text 及其确定性检查

gaps
  由 State + 检查结果确定性派生

round
  由 mea-loop plugin 管理

success/completed
  只能由满足公开 acceptance 的最终状态决定
```

State 更新发生在当前任务的运行过程中。Reducer 不扫描已有 trace 文件，也不从离线评测产物补写运行时 State。

模型的自然语言不是 State 的权威来源。

例如模型说：

```text
“这个商品是粉色，价格也符合，我买好了。”
```

只能作为模型输出，不得直接写入：

```text
facts.clicked_option
facts.displayed_price
decision.completed
```

## 3.2 可观察性不变量

Task State 中所有关于用户、商品、规格、价格和页面的语义事实，都必须是购物模型、未来的 Manager 或未来的 Auditor 通过合法输入能够知道的内容。它不要求每个角色都看过完整历史，但要求事实能够追溯到当前用户输入、`ask_shopper` 回复、实时 tool call 或模型可见 text。

MEA 自己产生的 `round_id`、调用计数、版本号、白名单和 evidence id 属于控制账本，不是购物答案，可以由 plugin 直接维护。

未来如果加入独立 Manager/Auditor LLM，只能向它们提供这一公开 State 和相应 evidence；不能因为换了模型角色就扩大到隐藏环境字段。

---

# 4. Round 的含义

## 4.1 Round 不是新的 ShopSimulator 环境

每个 round 是一个新的**认知子任务**，不是一次环境 reset。

```text
round 1: 同一个 env session
round 2: 同一个 env session
round 3: 同一个 env session
```

允许：

```text
每轮重新压缩模型工作记忆
```

禁止：

```text
每轮 reset ShopSimulator
```

否则下一轮会丢失购物页面、已选规格和环境状态，MEA 就失去长程状态价值。

## 4.2 一个 round 必须有四个东西

```text
goal
allowedTools
maxToolCalls
acceptanceChecks
```

例如：

```js
{
  goal: "核验候选商品身份和公开报价",
  allowedTools: ["click", "search"],
  maxToolCalls: 5,
  acceptanceChecks: ["product_id_observed", "price_known"]
}
```

## 4.3 Round 的结束条件

任意一个成立即可结束：

```text
1. acceptanceChecks 全部 pass
2. 达到 maxToolCalls
3. 环境已经 terminal
4. 工具连续失败/不可用
5. 用户需求发生变化
6. 模型提交 round report
7. DSH 进程结束
```

不要用“模型说完成了”作为唯一结束条件。

---

# 5. 数据持久化：只保存三种 MEA 产物

路径必须由 plugin/runtime 决定，不能由模型参数决定。

```text
<run-or-profile-state-dir>/mea/state.json
<run-or-profile-state-dir>/mea/rounds.jsonl
<run-or-profile-state-dir>/mea/evidence.jsonl
```

## 5.1 `state.json`

当前 State 的快照。

写入时：

```text
write temporary file
→ flush
→ rename
```

避免进程崩溃留下半个 JSON。

## 5.2 `rounds.jsonl`

每个 round 一行：

```json
{
  "round": 1,
  "goal": "核验候选商品身份和公开报价",
  "allowed_tools": ["click", "search"],
  "max_tool_calls": 5,
  "tool_calls": 2,
  "status": "complete",
  "audit": {
    "completion": "complete",
    "integrity": "clean",
    "checks": {
      "product_open_click_observed": "pass",
      "displayed_price_known": "pass"
    },
    "remaining_gaps": []
  },
  "evidence_refs": ["ev-r1-1", "ev-r1-2"]
}
```

## 5.3 `evidence.jsonl`

保存本轮在线发生的模型可见事件，不保存 `state`、`raw` 或完整 `presentationMeta`：

```json
{
  "evidence_id": "ev-r1-2",
  "round": 1,
  "source": "live_tool_event",
  "kind": "tool_observation",
  "read_sequence": 4,
  "tool_name": "click",
  "tool_arguments": {"value": "766004405772"},
  "model_visible_text": "...价格: 168...可点击的按钮: [...]",
  "extracted_claims": [
    {"key": "opened_candidate", "value": "766004405772", "status": "observed"},
    {"key": "displayed_price", "value": 168, "status": "observed"}
  ]
}
```

`evidence.jsonl` 是在线 MEA 自己写出的产物，不是下一次运行要读取的历史 trace。下一轮读取的是本任务当前的 `state.json` 和上一轮 audit 摘要。

---

# 6. 最小 Reducer

Reducer 是唯一允许修改 State 的函数：

```text
reduce(state, event) → newState
```

第一版只支持以下事件：

```text
TASK_INITIALIZED
OBSERVATION_RECEIVED
CANDIDATE_SEEN
FACT_OBSERVED
REQUIREMENT_RECEIVED
REQUIREMENT_CHANGED
ROUND_STARTED
ROUND_COMPLETED
ROUND_BLOCKED
TASK_COMPLETED
TASK_BLOCKED
```

Reducer 要求：

```text
输入旧 State
输出新 State
不原地修改旧 State
事件处理确定性
重复 observation 不造成无意义状态爆炸
```

## 6.1 不要加入复杂 transaction State

第一版不实现：

```text
PurchasePlan
Capability
Idempotency
COMMIT_UNKNOWN
TransactionService
```

原因很简单：当前目标是验证 MEA 在 Shop 场景下的数据流，不是实现可靠支付。

`buy now` 在第一版只允许沿用已有 harness 行为；如果无法根据公开状态确认最终购买事实，结果就是：

```text
unknown / blocked
```

而不是继续造一个购买事务子系统。

---

# 7. 确定性 Auditor：先小而可靠

第一版只对实时可见输入做这些检查：

```text
page_kind_observed
product_open_click_observed
displayed_price_known
option_click_observed
episode_finished_observed
```

实现要求：

- 精确值比较；
- 从实时 tool name / arguments 和模型可见 text 检查；
- 证据必须在当前 round 结束前产生；
- `ask_shopper` 回复不能作为商品证据；
- 不支持的语义返回 `unknown`；
- `unknown` 不得自动变成 `pass`。

### 例子

```text
expected: 粉色
actual: 非粉色
结果: fail
```

```text
expected: 粉色
actual: 本轮没有成功发生 click[粉色]，页面文本也没有可支持的选中结果
结果: unknown
```

```text
页面曾经显示价格 168
但当前候选/规格没有最终选定
结果: candidate evidence，有关最终满足为 unknown
```

---

# 8. Manager 在第一版怎么做？

第一版不要引入独立 Manager LLM。

在 DSH 的模型上下文中，mea-loop 只提供一个很短的当前 round 控制块：

```text
MEA 当前状态摘要：
- 当前 round：1
- 本轮目标：核验候选商品身份和公开报价
- 当前页面：搜索结果页
- 已知候选：0
- 已验证事实：0
- 当前缺口：需要候选
- 本轮允许工具：search, click
- 本轮最多工具调用：5
- 完成本轮的条件：发现候选并取得公开价格

完成本轮后停止继续购物，并提交本轮结果。
```

下一轮摘要由 State 生成：

```text
MEA 当前状态摘要：
- 当前 round：2
- 本轮目标：核验当前候选的规格
- 当前候选：ASIN ...
- 已验证价格：168
- 待核验：颜色分类
- 本轮允许工具：click
```

如果 DSH 没有可靠的运行时上下文替换/新 round 机制：

1. 不要用 CLI 重启进程伪造 MEA；
2. 不要声称已经实现 fresh round；
3. 先实现 round ledger、工具 guard、State reducer 和受控 `mea_round_report`；
4. 在 DSH 支持的消息/上下文 hook 上实现摘要注入；
5. 如果上下文 hook 不存在，报告“round 状态已实现，但自动上下文切换受 DSH API 限制”。

这是重要边界：不能用一个自定义命令行 runner 冒充 DSH plugin 内的 round。

---

# 9. 具体 Case A：简单任务，无需澄清

用户公开 Query：

```text
买一个黑色无线鼠标，预算 100 元以内。
```

假设环境中有候选商品。

## A0 初始化

```text
State:
requirements = [
  {key: "color", value: "黑色", status: "active", version: 0},
  {key: "budget", value: {upper: 100}, status: "active", version: 0}
]
candidates = []
facts = []
round = 0
```

plugin 在当前 DSH session 启动时直接接收用户公开 Query 并初始化 State；随后等待实时工具事件，不读取任何已有 trace。

## A1 Discover round

```text
round = 1
goal = "寻找符合公开类别和预算线索的候选"
allowedTools = ["search", "click"]
maxToolCalls = 5
```

模型调用：

```text
search[黑色 无线鼠标]
```

`shop-tools` 返回公开搜索结果。

plugin 做：

```text
写 evidence ev-1
提取候选 ASIN/title/公开价格
State.candidates += candidate
```

## A2 Inspect round

下一轮 State：

```text
candidates = [鼠标 ASIN A]
gap = need_product_evidence
```

round 2：

```text
goal = "核验候选 A 的商品身份、黑色规格和价格"
allowedTools = ["click"]
maxToolCalls = 4
```

模型调用：

```text
click[A]
click[黑色]
```

模型实时看到的工具结果包含：

```text
商品标题：...
价格: 89
可点击的按钮: [..., "黑色", "buy now"]
```

Auditor：

```text
product_open_click_observed(A)   pass（来自 click[A]）
option_click_observed(黑色)       pass（来自 click[黑色]）
displayed_price_known             pass（来自该次工具返回 text）
episode_finished_observed         false
```

Reducer：

```text
facts += opened_product / clicked_option / displayed_price
candidate A → inspected
```

## A3 结束

如果本轮只完成了候选核验：

```text
round.status = complete
candidate.status = inspected
task decision 仍为 running / unknown
```

如果当前任务的成功定义必须是实际购买，而没有可用公开回执：

```text
decision = unknown
reason = purchase_not_implemented_or_receipt_missing
```

不能因为页面看起来满足就把任务标成已购买。

---

# 10. 具体 Case B：916 类型——候选超预算，需要向用户澄清

这是已有数据中最值得参考的类型：

```text
初始需求：预算 150 元左右
候选报价：168 元
用户回复：168 元超预算，最多 165 元
```

重点不是把 916 写死，而是展示数据流。

## B0 初始 State

```text
requirements:
  budget = 150 元左右
  status = active
  version = 0

candidates = []
facts = []
gaps = [need_candidate]
```

## B1 Inspect candidate

模型找到候选 A，打开详情页。

实时公开证据（来自 `click[A]` 及其模型可见返回文本）：

```text
tool call = click[A]
返回文本包含商品 A 的页面内容
返回文本包含价格 168
```

Reducer 写入：

```text
candidate A = inspected
fact price(A) = 168
```

Auditor 计算：

```text
168 > 当前预算上限
```

注意：这是一个公开确定性检查，不是模型自报。

State 派生出：

```text
gap = need_user_clarification
reason = quote_outside_current_budget
```

## B2 Clarify round

round 2 不让模型继续搜索或点击，而是：

```text
goal = "询问用户是否接受候选 A 的具体 168 元报价"
allowedTools = ["ask_shopper"]
maxToolCalls = 1
```

模型/插件调用：

```text
ask_shopper("候选 A 当前公开报价 168 元，超出当前预算。是否接受这个具体报价？")
```

Shopper 回复：

```text
168 元超预算了，我最多接受 165 元。
```

plugin 只把它作为 requirement event：

```json
{
  "type": "REQUIREMENT_CHANGED",
  "key": "budget",
  "value": {"upper": 165},
  "source": "shopper_reply",
  "version": 1
}
```

如果用户同时拒绝候选 A：

```text
candidate A → rejected
scope = candidate A
```

不能把同类商品全部拒绝。

## B3 新 State

```text
requirements:
  budget v0 → superseded
  budget v1 = 165

candidate A = rejected

gaps:
  need_candidate = open
  reason = rejected_candidate
```

旧 round 的目标已经失效，不能继续在 A 上点击。

## B4 Discover alternative round

```text
round = 3
goal = "在当前预算 165 元以内寻找另一个候选"
allowedTools = ["search", "click"]
maxToolCalls = 5
```

如果找不到新的公开候选：

```text
decision = blocked
reason = no_supported_candidate_in_explored_scope
```

注意这个结论只表示：

```text
在当前已探索范围内没有公开支持的候选
```

不能声称整个商城都不存在满足商品。

---

# 11. 具体 Case C：需求没有歧义，但规格/价格证据不足

用户 Query：

```text
买一件粉色外套，预算 300 元以内。
```

模型打开商品页，但只看到：

```text
可选颜色：[红色, 黑色]
价格：从 199 元起
```

## C1 Auditor 结果

```text
color=pink       fail 或 unknown（取决于公开选项是否明确排除粉色）
price_known      unknown（只有起价，不是当前选中规格价格）
```

不能做：

```text
把 199 元起价当成最终价格
把商品标题中“时尚”当成粉色
让模型自报“应该有粉色”成为 pass
```

State：

```text
gaps = [
  {key: "need_option", status: "open"},
  {key: "need_price", status: "open"}
]
```

如果没有更多合法公开动作能够解决：

```text
decision = blocked_unknown
```

这不是失败的实现，而是正确的 epistemic boundary：证据不足就保持 unknown。

---

# 12. 具体 Case D：用户明确拒绝当前候选，但不拒绝同类商品

已有数据中出现过“拒绝当前商品/款式、寻找其他款式”的场景。

初始：

```text
candidate A = inspected
```

用户回复：

```text
这个款式不要，换一个。
```

plugin 生成：

```json
{
  "type": "REQUIREMENT_EVENT",
  "kind": "reject",
  "scope": {"candidate": "A"},
  "source": "shopper_reply"
}
```

Reducer：

```text
A.status = rejected
```

但不能做：

```text
把 color / category / brand 全局标成 rejected
把所有候选都清空
把用户的“不要这个”解释成“不需要这个品类”
```

下一 round：

```text
goal = "寻找不属于被拒范围的新候选"
```

这是 MEA 中“State 改变后重新选择子任务”的核心例子。

---

# 13. 具体 Case E：终局与模型假声明

模型输出：

```text
我已经买好了。
```

但当前实时可见事实只有：

```text
最后一次工具返回仍是普通商品详情页文本
页面显示价格 168
没有出现模型可见的 Episode finished
没有公开购买回执
```

plugin 必须写：

```text
decision = unknown
reason = model_claim_not_supported_by_public_evidence
```

不能写：

```text
task_success = true
```

反过来，如果工具已经向模型返回：

```text
Episode finished.
```

MEA 就把当前任务标记为不可继续行动。如果模型还尝试：

```text
click[Buy Now]
```

`mea-loop` 应该在 pre-execute 拒绝：

```text
reason = environment_terminal
```

并记录：

```text
round.status = blocked
```

这里运行时只根据模型可见的 `Episode finished` 停止后续动作，不读取后台 `termination_reason`，也不把“episode 已结束”解释为“购买正确”。最终购买是否正确仍由任务结束后的离线评测判断。

---

# 14. `ask_shopper` 的数据流

`ask_shopper` 有两个不同结果，不能混在一起：

```text
用户回复文本
  → 当前工具调用当场返回给模型
  → MEA 同时把这次模型可见回复保存为在线 evidence
  → 仅在能够可靠解析时生成 requirement event
```

例如：

```text
用户回复：预算最多 165 元
```

MEA 只在当前回复含义明确且第一版解析规则能够可靠处理时更新：

```text
REQUIREMENT_CHANGED(budget=165, version=1)
```

如果只是：

```text
“再看看吧”
```

则不能随便推导新的预算或规格；可以记录：

```text
open question / unknown
```

如果 DSH 的 `ask_shopper` 当前只返回 reply，没有结构化事件，第一版不要在 plugin 里重复做复杂中文语义解析。可以：

1. 先把 reply 记录为 interaction evidence；
2. 只有明确的确定性事件接口存在时才更新 requirements；
3. 复杂语义保持 unknown；
4. 把事件抽取作为后续独立扩展。

整个过程发生在同一次在线任务里：`ask_shopper` 一返回就处理，不从任务结束后的 trace 回放或重建需求变化。

---

# 15. Plugin 内部最小伪代码

以下是语义伪代码，不代表 DSH API 名称：

```js
export function apply(ctx, config) {
  // 当前任务开始时由公开用户输入创建；不得从历史 trace 重建。
  const state = initializeFromCurrentUserRequest(ctx, config)
  let currentRound = state.round

  onToolPreExecute(ctx, async (call, next) => {
    if (!isShoppingTool(call.name)) return next()

    if (state.decision.kind !== "running") {
      return deny("MEA task is no longer running")
    }

    if (state.round.status !== "running") {
      return deny("current MEA round is closed")
    }

    if (!state.round.allowedTools.includes(call.name)) {
      return deny("tool is outside current MEA round")
    }

    if (state.round.toolCalls >= state.round.maxToolCalls) {
      closeRound("budget_exhausted")
      return deny("current MEA round budget exhausted")
    }

    state.round.toolCalls += 1
    persistState(state)
    return next()
  })

  onToolPostExecute(ctx, async (call, result, next) => {
    if (!isShoppingTool(call.name)) return next()

    // 只消费本次实时调用以及本次实际返回给模型的 text。
    // 不读取 result.value.state / raw / presentationMeta。
    const visibleEvent = {
      toolName: call.name,
      toolArguments: call.arguments,
      modelVisibleText: result.value?.text ?? ""
    }
    const evidence = appendEvidence(visibleEvent, state.round.number)
    reduceVisibleEvent(state, visibleEvent, evidence)

    const checks = auditCurrentRound(state, evidence)
    if (allAcceptanceChecksPass(checks)) {
      closeRound("acceptance_satisfied", checks)
      persistRound(state, checks)
      prepareNextRound(state)
    } else if (state.round.toolCalls >= state.round.maxToolCalls) {
      closeRound("budget_exhausted", checks)
      persistRound(state, checks)
      prepareNextRound(state)
    }

    persistState(state)
    return next()
  })

  registerControlledRoundReport(ctx, {
    execute(report) {
      // report 只能结束当前 round，不能直接宣布 task success
      closeRoundFromReport(state, report)
      persistState(state)
      return reportAcknowledged()
    }
  })
}
```

实际执行前必须把 `onToolPreExecute`、`onToolPostExecute`、工具注册和上下文注入替换成当前 DSH 真实 API。不能直接复制上述伪代码当作已实现。

---

# 16. 第一版实现边界

## 必须实现

```text
1. DSH profile 中注册 mea-loop plugin
2. 在当前 DSH session 开始时从用户初始公开需求初始化 State
3. 从实时 tool name / arguments 和返回给模型的 text 构造 visible event
4. 一个最小 State
5. round start / count / close
6. allowedTools 和 maxToolCalls guard
7. deterministic checks
8. State reducer
9. state.json / rounds.jsonl / evidence.jsonl 持久化
10. finish/terminal/unknown 不误报 success
11. h0/h1 不被修改
12. 一个单任务 DSH 冒烟测试
13. 证明运行时不读取历史 trace、rubric、judgment、report、state/raw/presentationMeta
```

## 明确不实现

```text
1. 独立 CLI MEA runtime
2. Python Controller
3. 独立 Manager LLM
4. 独立 Auditor LLM
5. Shopping Purchase Plan
6. 购买事务/幂等系统
7. capability 或额外交易安全层
8. 复杂中文语义事件抽取
9. 200 条正式实验
10. 真实购买
```

---

# 17. 验收用例清单

至少用 fake DSH / plugin fixture 覆盖：

```text
Case A:
  search → candidate → inspect → price/option evidence

Case B:
  168 元超预算 → ask_shopper → budget change → old target invalid

Case C:
  起价/规格不明确 → unknown，不猜

Case D:
  reject candidate A → 只拒绝 A → 下一轮寻找替代

Case E:
  模型声称买好但无公开回执 → unknown

Case F:
  terminal 后继续工具调用 → pre-execute 拒绝

Case G:
  达到 round tool budget → round close，不继续调用

Case H:
  重新加载 state.json → State 可继续读取，不依赖完整 transcript

Case I:
  给历史 trace 放入诱导性隐藏答案 → 在线 MEA 完全不读取，State 不受影响
```

测试应从 DSH plugin 注册/调用路径开始，不能只测试一个脱离 DSH 的 Node/ Python 纯函数。

---

# 18. 执行 Agent 必须先确认的三个问题

在写代码前，先回答并记录：

### 问题 1：当前 DSH 是否支持 round boundary？

例如：

- 是否能在 post-tool hook 中阻止下一次模型 tool call？
- 是否能向下一次模型请求注入新的摘要？
- 是否能在一个任务内创建新的模型上下文而不 reset ShopSimulator？
- 是否能注册一个只结束当前 round 的受控工具？

如果不支持全部能力，不要用 CLI 重启假装支持。准确报告支持到哪一层。

### 问题 2：当前用户请求和模型可见工具结果怎样从 DSH hook 获取？

确认 `mea-loop` 能在当前任务启动和实时工具执行中拿到：

```text
用户初始公开请求
exec.name
exec.arguments
result.value.text（与模型实际收到的内容一致）
```

不要为了方便改读 `result.value.state`、`result.value.raw`、presentationMeta、session logger 或落盘 trace。如果 hook 暂时拿不到模型可见 text，应调整 `shop-tools` 与 `mea-loop` 之间的在线公开接口，使同一份 text 同时交给模型和 MEA；不能退回历史 trace 回放，也不能把后台字段暴露给模型。

### 问题 3：DSH profile 如何隔离？

确认：

```text
h0/h1 不注册 mea-loop
MEA profile 注册 mea-loop
```

MEA 的 system prompt、工具和 guard 不能意外进入 h0/h1。

---

# 19. 交付报告

执行完成后必须报告：

1. `mea-loop` plugin 注册位置；
2. 实际使用了哪些 DSH hook；
3. 每种 hook 的输入输出；
4. State 字段和 reducer 事件；
5. round 如何开始、计数、结束；
6. 下一轮如何得到 State 摘要；
7. 用户初始需求和实时模型可见 evidence 怎样进入 Reducer；
8. 哪些字段明确不会进入模型上下文；
9. Case A–I 哪些通过；
10. 是否真实启动 DSH/ShopSimulator；
11. h0/h1、Judge v1/v2 是否未修改；
12. `run_mea.py` 是否没有成为生产入口；
13. 尚未实现的上下文切换、购买或语义能力。
14. 运行时如何证明没有读取历史 trace 和评测产物。

如果 DSH 不支持某个关键 hook，应报告限制并停止扩展，不要绕过 DSH 改成 CLI 或独立服务。

---

# 20. 最终判断标准

实现成功不是“文件多了”或“测试数量多了”，而是下面的数据流真实成立：

```text
current user request
  → mea-loop initializes external Task State
  → DSH tool call happens online
  → mea-loop observes tool name / arguments and the same text returned to the model
  → reducer updates external Task State
  → current round closes at a bounded condition
  → next round uses State summary, not full transcript
  → deterministic audit distinguishes pass/fail/unknown
  → model claims never directly become facts
```

历史 h0 trace 只用于说明为什么需要这些机制；它不出现在上述运行数据流中。

如果只能实现：

```text
工具调用记录 + State 持久化 + bounded guard
```

但 DSH 当前 API 不支持真正的上下文切换，也必须如实称为：

```text
MEA loop control/evidence skeleton
```

不能称为完整的论文式多轮 MEA。
