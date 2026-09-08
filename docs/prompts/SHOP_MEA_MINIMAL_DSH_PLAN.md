# Shop 场景 MEA 最小实现方案（DSH Plugin Only）

> 给另一个 coding agent 执行。
>
> 目标：参考论文的 **MEA 核心思想**，在当前 Shopping Harness 上实现一个最小、可解释、可回放的 MEA loop。
>
> 重要约束：本项目底座是 DSH。生产入口必须是 DSH profile/plugin。不要把 CLI、Python runner、评测脚本或论文中的文件名当成产品架构。

---

## 1. 先说结论：只实现一个 Plugin 级 MEA Loop

当前不需要重新设计整个 Shopping Harness，也不需要引入一套独立的 Python orchestration 系统。

最小可行结构：

```text
DSH profile
  ├── shop-tools plugin       # 已有：search / click / finish / ask_shopper
  ├── buy-guard plugin        # 已有：购买前页面条件检查
  └── mea-loop plugin         # 新增：状态、轮次、边界、记忆、终止

mea-loop plugin
  ├── 收集 shop-tools 的工具结果元数据
  ├── 维护当前任务 State
  ├── 在 DSH 上下文中注入当前 State / 下一步目标
  ├── 控制一轮 MEA 子任务的边界
  ├── 触发下一轮或终止
  └── 持久化可回放的 MEA state / round log
```

不要先做：

```text
Python Controller
独立 Manager 服务
独立 Auditor 服务
Purchase Plan
TransactionService
capability / idempotency_key
新的 CLI 入口
新的评测协议
```

这些都不是 MEA 核心的必要条件。购买事务以后如果真的需要，再作为 Shopping 业务扩展单独设计。

---

## 2. 论文思想在本项目中的最小映射

不要模仿论文的类名，保留论文真正重要的因果链：

```text
外置 Task State
    ↓
Manager 根据 State 选择下一项工作
    ↓
Bounded sub-task / round
    ↓
DSH Executor 执行有限工具调用
    ↓
从环境公开观察收集事实
    ↓
独立规则检查本轮结果
    ↓
把事实写回 State
    ↓
下一轮 Manager 读取更新后的 State
```

在 DSH plugin 中对应为：

| 论文思想 | DSH 实现 |
|---|---|
| Task State | mea-loop plugin 持有并持久化的任务 JSON 状态 |
| Manager | 通过 DSH 当前模型上下文做下一步决策；第一版可用固定规则/提示词，不必新增 Manager 服务 |
| Executor | 当前 DSH 模型 + 已有 shop-tools |
| Bounded sub-task | plugin 注入的当前 round goal、允许动作和停止条件 |
| Evidence | shop-tools 返回的 `presentationMeta` / raw tool result 中的公开观察字段 |
| Auditor | plugin 内确定性检查器，先只检查页面、候选、规格、价格、终止状态 |
| Reducer | plugin 内唯一的 State 更新函数 |
| 下一轮 | 在 round boundary 注入新的摘要和目标，而不是把完整 transcript 无限累积 |

第一版的关键不是“多 Agent”，而是：

> **把连续购物行为切成若干个有明确目标的 round，并让下一轮依赖结构化 State，而不是依赖上一轮完整对话。**

---

## 3. 先分析已有数据，再定义 State

已有数据已经说明当前 Shopping Harness 的事实形态：

### 3.1 当前工具数据流

`src/shop-tools.js` 已经把模型可见信息和评测元数据分开：

```text
ShopSimulator response
  ├── instruction / text       → 模型可见
  └── state/raw                → presentationMeta，供评测读取
```

公开工具事实目前包括：

```text
observation_state
progress
done
termination_reason
reward / reward_valid       # 这些只能留在评测侧，不进入 MEA 模型上下文
purchase
```

MEA plugin 可以读取工具结果元数据，但给模型的新 State 只能使用公开字段：

```text
page_type
search_available
actions
selected_options
available_options
selected_price
product: asin/title/brand/category/key_attributes
```

不要把 reward、goal、gold ASIN、known_valid_asins、termination_reason 重新注入模型上下文。

### 3.2 Rubric v2 数据给出的设计事实

已有 200 条历史任务的 v2 数据已经说明：

- 初始 Query 约束和后续 Shopper 澄清不是同一时刻生效；
- 需求会 `reveal / add / modify / conditional_accept / reject / revoke`；
- 同一个需求可能被修改或撤销；
- 候选页出现过匹配信息，不等于最终决定满足；
- 没有购买或明确最终候选时，最终满足应为 `unknown`，不能凭候选页自动判成功；
- ask_shopper 回复是需求来源，不是商品证据；
- 证据必须带出现位置，不能引用决策之后的信息；
- 当前 v2 Judge 的核心是“需求时间线 + 决策时刻 + 可见证据”。

因此 MEA State 不应只是：

```text
last_message
last_page
last_action
```

而应至少包含：

```json
{
  "schema": "shopping-mea-state-v1",
  "task": {
    "run_id": "...",
    "task_id": "...",
    "env_session": "..."
  },
  "requirements": [
    {
      "key": "budget",
      "value": "...",
      "source": "initial_query|shopper",
      "status": "active|superseded|revoked|unknown"
    }
  ],
  "candidates": [
    {
      "asin": "...",
      "title": "...",
      "status": "seen|inspected|rejected|selected",
      "evidence_refs": ["..."],
      "last_observed_round": 1
    }
  ],
  "facts": [
    {
      "key": "selected_price",
      "value": 123,
      "status": "observed|unknown",
      "evidence_ref": "...",
      "round": 1
    }
  ],
  "open_questions": [],
  "current_observation": {
    "page_type": "...",
    "selected_options": {},
    "selected_price": null,
    "product": null,
    "env_revision": 0
  },
  "round": {
    "number": 0,
    "goal": "...",
    "status": "not_started|running|complete|incomplete|blocked"
  },
  "decision": {
    "kind": null,
    "position": null
  },
  "stop_reason": null
}
```

这是最小业务 State，不是完整评测 rubric，也不是完整购物交易账本。

---

## 4. State 的最小权威规则

### 4.1 State 由 plugin 管理，不由模型直接写

模型只能通过工具行动产生事实：

```text
模型调用 search/click/ask_shopper
  → ShopSimulator 返回结果
  → mea-loop plugin 读取公开结果
  → reducer 更新 State
```

模型不能：

- 直接写 state 文件；
- 通过文本声称某个商品已经满足约束；
- 直接把候选标成 selected；
- 直接把任务标成 success；
- 直接修改预算或需求版本。

### 4.2 不把模型 claim 当事实

模型输出：

```text
“这个商品符合要求，我买好了。”
```

只能记录为普通文本/诊断信息。

只有公开环境观察能够证明：

```text
商品身份
选中的规格
当前价格
购买结果/终止状态
```

才更新对应 State fact。

### 4.3 需求来源和商品证据分开

```text
ask_shopper response → requirement event
search/click observation → product evidence
```

不能用用户回复中的“我接受”直接证明商品价格或规格，也不能用候选标题直接证明成交。

### 4.4 版本最小化

第一版只保留三个版本：

```text
state_version      # MEA State 更新次数
requirement_version # 当前有效需求版本
observation_version/env_revision # 环境观察版本，若环境提供
```

如果环境没有可靠 `env_revision`，使用 plugin 自己的 observation sequence，但必须明确这是“读取序号”，不是环境动作序号。

不要先引入十几种版本、hash、transaction 状态。

---

## 5. 最小 MEA Loop

### Round 0：Bootstrap

plugin 初始化：

```text
读取任务身份
读取公开初始 Query
读取第一次公开观察
初始化 State
```

模型上下文只增加一小段：

```text
你正在执行一个有边界的购物任务。
当前 round：bootstrap
目标：读取并理解当前页面，不购买，不结束任务。
```

Bootstrap 不要让模型额外操作环境；读取工具结果即可。

### Round 1：Discover / Inspect

Manager 根据 State 选择一个目标，例如：

```text
寻找候选
```

或：

```text
核验当前候选的商品身份、规格、价格
```

round 必须包含：

```text
round_goal
allowed_tools
max_tool_calls
stop_conditions
acceptance_checks
```

第一版可以由固定规则生成，不需要 LLM Manager。

例如：

```json
{
  "round_goal": "核验当前候选的公开商品信息",
  "allowed_tools": ["click", "search"],
  "max_tool_calls": 5,
  "stop_conditions": ["evidence_collected", "terminal", "budget_exhausted"],
  "acceptance_checks": ["product_seen", "price_seen"]
}
```

### Round 结束

以下任意情况结束 round：

- acceptance checks 已满足；
- 达到工具调用上限；
- 页面终止；
- 模型主动报告无法继续；
- 环境错误/工具错误；
- 需求发生变化。

round 结束后 plugin：

1. 读取最后公开观察；
2. 运行确定性检查；
3. 只把通过检查的事实写入 State；
4. 写 round summary；
5. 清理/压缩当前 round 的上下文；
6. 生成下一轮目标。

### Round 2：Clarify

只有在 State 中存在真实的用户歧义时才调用 `ask_shopper`。

问题要带具体上下文，但不能让用户替代环境取证：

```text
当前候选为 X，公开报价为 Y，是否接受这个具体报价？
```

收到回复后：

```text
Shopper reply
  → requirement event
  → 更新 requirements
  → 旧 round plan 失效
  → 重新计算下一 round
```

如果同步失败或回复含义不确定：

```text
记录 pending/unknown
不要假设需求已经变化
不要继续依据未确认的需求执行危险动作
```

### Round 3：Select / Verify

如果用户确认了具体候选，下一轮只做：

```text
选择规格
核验规格
核验价格
```

不能同时搜索大量新候选、修改需求和尝试购买。

### Terminal

终止不是成功。

第一版只允许以下终止类型：

```text
completed_with_evidence
no_supported_candidate
user_declined
environment_terminal
budget_exhausted
blocked_unknown
```

是否“购物成功”必须由环境公开回执或明确最终页面事实决定。仅有：

```text
模型说买好了
工具调用成功
页面曾经出现过商品
```

都不能单独把 State 标成成功。

第一版可以完全不实现自动购买；如果没有可靠购买回执，就输出：

```text
blocked_unknown / no_supported_candidate
```

---

## 6. Auditor 的最小范围

不要一开始做 LLM Auditor。Shop 场景第一版的 Auditor 只做确定性检查：

```text
page_type_equals
product_id_equals
selected_option_equals
price_known
terminal_false
```

检查要求：

- 精确比较，不做子串匹配；
- 从最新公开 observation 检查；
- evidence 必须在当前 round 或更早；
- ask_shopper 结果不算商品 evidence；
- 不支持的语义返回 `unknown`；
- unknown 不得被转成 pass。

例如：

```text
expected = "红色"
actual = "非红色"
结果 = fail
```

复杂语义先保持 unknown：

```text
“适合户外使用”
“药效好”
“风格符合”
```

不要用 LLM 的一句话把 unknown 强行变成 satisfied。

---

## 7. DSH Plugin 的实现边界

### 7.1 新增一个 `mea-loop` plugin

建议文件：

```text
src/mea-loop.js
```

通过 bundle/package exports 暴露：

```json
{
  "exports": {
    ".": "./src/shop-tools.js",
    "./buy-guard": "./src/buy-guard.js",
    "./mea-loop": "./src/mea-loop.js"
  }
}
```

在 DSH profile patch 中注册：

```yaml
- insert:
    - id: shop-tools
      name: '@shopping-longhorizon/shop-tools'
    - id: buy-guard
      name: '@shopping-longhorizon/shop-tools/buy-guard'
    - id: mea-loop
      name: '@shopping-longhorizon/shop-tools/mea-loop'
```

实际 plugin hook/API 必须以当前 DSH 版本文档和现有 `buy-guard` 的写法为准，不要臆造 hook 名称。

### 7.2 Plugin 只观察和控制自己的 State

它可以：

- 监听工具调用前后事件；
- 读取 `result.value.state` / `presentationMeta` 中的公开工具状态；
- 维护 round 和 State；
- 注册必要的受控工具，例如 `mea_round_report`；
- 在下一轮提供摘要上下文；
- 在越界时拒绝工具调用；
- 持久化自己的日志。

它不可以：

- 读取环境内部 goal/reward/gold；
- 访问评测 rubric 作为在线答案；
- 修改 ShopSimulator 隐藏目标；
- 修改 DSH 核心；
- 接管 `shop-tools` 的环境协议；
- 通过任意文件/bash 工具让模型操作 State。

### 7.3 与现有插件的关系

```text
shop-tools 负责：工具注册、环境调用、模型可见文本、工具元数据
buy-guard   负责：现有 Buy Now 页面条件
mea-loop    负责：round、State、目标、边界、Reducer、终止
```

不要把 MEA 逻辑复制到 `shop-tools.js`，也不要把 ShopSimulator 业务逻辑复制到 `mea-loop.js`。

---

## 8. 持久化：只落盘三类文件

第一版只需要 plugin 负责落盘：

```text
mea/state.json
mea/rounds.jsonl
mea/evidence.jsonl
```

### `state.json`

当前 State 的原子快照。

### `rounds.jsonl`

每轮一条：

```json
{
  "round": 1,
  "goal": "核验候选价格",
  "started_at": "...",
  "ended_at": "...",
  "tool_calls": 3,
  "status": "complete",
  "checks": {
    "product_seen": "pass",
    "price_known": "pass"
  },
  "evidence_refs": ["ev-1", "ev-2"]
}
```

### `evidence.jsonl`

只保存公开观察的必要白名单字段：

```json
{
  "evidence_id": "ev-2",
  "round": 1,
  "source": "shop-tools",
  "kind": "observation",
  "observation": {
    "page_type": "product_detail",
    "selected_options": {},
    "selected_price": 168,
    "product": {"asin": "...", "title": "..."}
  }
}
```

不要先设计完整 event-sourcing、数据库、事务日志、可信目录、capability store。

写文件时使用临时文件 + rename，避免半写快照；文件路径由 plugin/runtime 注入，不能让模型指定。

---

## 9. 与现有 v1/v2 Judge 的关系

MEA plugin 与 `eval/` 评测脚本是两个方向：

```text
DSH plugin：在线控制和 State 流转
Judge v1/v2：离线评估 model-visible trajectory
```

MEA plugin 可以产生更结构化的 round/evidence 日志，但不能依赖：

```text
eval/judge.py
eval/judge_v2.py
rubrics
hidden goal
gold answer
```

评测脚本可以在后处理阶段消费：

```text
raw trace
mea rounds
mea evidence
```

但在线 MEA 不读取离线 rubric 来决定下一步。

历史 Judge v2 已经验证的原则必须保留：

- decision-time boundary；
- candidate evidence 与 final decision 分离；
- requirement lifecycle；
- evidence 不足时 unknown；
- post-terminal action 不被当作有效决策证据。

---

## 10. 最小插件验收测试

不要先测真实 LLM 购物成功率。先用 DSH plugin test/fake tool fixture 验证机制。

必须覆盖：

### A. State 流转

```text
初始化
→ bootstrap
→ discover/inspect
→ round summary
→ 下一轮读取 State
```

### B. Round 边界

- 达到最大工具调用数后拒绝继续动作；
- acceptance 满足后结束本轮；
- 当前 round 结束后不能继续使用旧 round 目标；
- 下一轮只收到 State 摘要，不注入上一轮完整 transcript。

### C. 证据和审计

- 工具返回的公开 observation 可以进入 evidence；
- reward/goal/gold 不进入 MEA context；
- Executor/模型文字 claim 不会变成 fact；
- unknown 不会被误判 pass；
- ask_shopper 回复不算商品证据；
- evidence 引用只允许当前决策时刻之前。

### D. 需求变化

```text
初始需求 v0
→ ask_shopper 产生明确 modify
→ requirements v1
→ 当前 round 失效
→ 下一轮读取 v1
```

重复/空/未确认回复必须有限停止，不得无限循环。

### E. 终止

- `finish` 不是自动 success；
- 没有最终候选时为 unknown/blocked；
- terminal 后不再产生新的购物动作；
- 发生工具错误时 round 标记 incomplete，不伪造完成。

### F. Profile 隔离

- h0/h1 原有行为不变；
- mea profile 注册 mea-loop；
- 未注册 mea-loop 的 profile 不出现 MEA 工具/提示；
- 通用 bash/fs/subagent 等工具仍按 profile 原规则处理。

---

## 11. 实施顺序

### Step 1：只读现有 DSH plugin API

阅读：

```text
src/shop-tools.js
src/buy-guard.js
cordis.patch.yml
harness/h0/cordis.patch.yml
harness/h1/cordis.patch.yml
package.json
```

确认：

- plugin 如何注册工具；
- plugin 如何监听 pre/post execute；
- output/presentationMeta 如何持久化；
- 如何给后续模型轮次增加上下文；
- 哪些 hook 真实存在。

如果 DSH 当前没有可靠的“自动切换 round / 重建上下文”能力，不要伪造。先实现：

```text
round state + bounded guard + controlled report tool + persistent logs
```

把 round transition 留在 DSH 可支持的边界内。

### Step 2：实现公开 State projector

新增纯 JS 模块或 `mea-loop.js` 内部函数：

```text
projectPublicObservation(result)
projectPublicRequirementEvent(result)
reduceState(state, event)
```

先用现有 `shop-tools` 返回结构，不改 ShopSimulator。

### Step 3：实现 round ledger

只做：

```text
start round
count tool calls
check stop condition
end round
write state/round/evidence
```

不要加入购买事务。

### Step 4：实现最小 deterministic auditor

只实现：

```text
product_id_equals
price_known
selected_option_equals
terminal_false
```

复杂语义返回 unknown。

### Step 5：通过 profile 注册

新建独立的 MEA profile，例如：

```text
harness/mea/
```

不要修改 h0/h1 的行为。MEA 作为独立 profile/plugin 能力存在。

### Step 6：用 fake DSH/plugin fixture 测试

测试必须从 DSH plugin 调用入口开始，不要只测试 Python helper。

如果必须保留 Python 脚本，只能作为 fixture runner，文件名和文档明确标注：

```text
development test harness only
```

### Step 7：单任务真实 DSH 冒烟

只跑一个无购买任务，验证：

```text
DSH profile
→ shop-tools
→ mea-loop
→ ask_shopper（如任务需要）
→ State/round/evidence 落盘
→ 环境 lease 正常释放
```

不跑真实购买，不跑 200 条。

---

## 12. 明确禁止的过度设计

本次执行 Agent 不要因为看到已有 E3 代码或论文术语而增加：

```text
Python Controller
StateStore + Reducer 多模块重型框架
ShoppingCoordinator
ContractCompiler
ModeConfig CLI
LLM Manager service
Hybrid Auditor service
Purchase Plan
TransactionService
capability
idempotency_key
trusted side-channel
transaction secret
新的 event-sourcing 数据库
```

第一版只需要一个 plugin 内部的小状态机。

如果未来发现必须支持真实购买，先另开设计文档；不要把支付/事务系统混进本次 MEA 核心实现。

---

## 13. 完成标准

### 设计

- [ ] 生产入口明确是 DSH plugin/profile；
- [ ] `run_mea.py`（如存在）只用于开发测试，不是产品入口；
- [ ] Task State、round、evidence、reducer 的职责清晰；
- [ ] MEA 通用机制与 Shopping 业务事实分开；
- [ ] 没有依赖 eval rubric / hidden goal / reward；
- [ ] 没有引入购买事务系统。

### 实现

- [ ] `mea-loop` 通过 DSH bundle/plugin 注册；
- [ ] h0/h1 行为不变；
- [ ] public observation projector 工作；
- [ ] round 边界和动作预算有效；
- [ ] 公开证据能持久化和回放；
- [ ] deterministic checks 能返回 pass/fail/unknown；
- [ ] 模型 claim 不会直接升级 State；
- [ ] 需求事件能使后续 round 读取新 State；
- [ ] finish/terminal/unknown 不会误报 success。

### 验证

- [ ] DSH plugin/fake fixture 测试通过；
- [ ] 单任务真实 DSH 冒烟通过，或明确记录 DSH API/环境依赖阻塞；
- [ ] 不运行真实购买；
- [ ] 不运行 200 条正式实验；
- [ ] 不修改 h0/h1 和 v1/v2 Judge/rubric 评测口径；
- [ ] 最终报告区分“机制测试通过”和“模型效果尚未验证”。

---

## 14. 执行 Agent 的交付报告

完成后只报告以下内容：

1. 新增/修改了哪些 DSH plugin/profile 文件；
2. plugin 的真实运行入口是什么；
3. State 的字段和数据流是什么；
4. round 如何开始、结束、进入下一轮；
5. evidence 如何从 shop-tools 元数据产生；
6. 哪些字段明确不会进入模型上下文；
7. 运行了哪些 plugin/fake DSH/真实 DSH 测试；
8. h0/h1 和 Judge v1/v2 是否未被修改；
9. 未实现的内容；
10. 仍然存在的风险。

如果无法确认 DSH 的某个 hook 或上下文注入机制，请停止并报告，不要用 CLI 或额外 Python 服务绕过 DSH plugin 设计。
