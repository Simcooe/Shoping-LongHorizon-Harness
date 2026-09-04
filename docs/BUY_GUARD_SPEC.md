# Buy Guard v1 实施单（交给执行会话）

> 仓库：`/Users/ywwl/shopping-longhorizon-harness/Shoping-Longhrizon-Harness`
> 分支：`main_buy_guard`（已建好，直接在上面改）
> 基线：`h0` profile（`run_batch.sh` 默认）。本实施单新增 `h1` = h0 + buy-guard。
> 全程不改任何提示词、不改 `src/shop-tools.js`、不改 h0。

---

## 0. 背景（为什么做）

32 条 M+P 基线（`runs/0903-18*`、`0903-19*`）分析结论：

- **6/32 任务死于同一模式**：模型选对了商品和规格 → 点进 description/features 子页面 → 在子页面点 `buy now` → 该页可点击列表只有 `["back to search", "< prev"]`，点击被环境**静默忽略**（不报错）→ 模型以为买成了，结束 → `done=False, reward=0`。
- 全量统计：424 次点击里 66 次（15.6%）点了当前页面不存在的按钮；`buy now` 只在 `page_type=product_detail` 的按钮列表里出现（213:0，零例外）。
- 根因：模型按内部计划行动，不核对当前观察。提示词治不了（工具描述里本来就写了"值必须逐字取自按钮列表"）。
- 方案：在工具层做**确定性拦截**——`buy now` 发出前用程序检查页面状态，不合法就拒绝并把纠正指引作为错误结果返回给模型。

---

## 1. dsh 拦截 API（已在本仓库 `deepseek-harness/` 0.1.2-alpha.5 源码中核实）

引用文件：`deepseek-harness/packages/core/tools/src/index.ts`
参考测试：`deepseek-harness/packages/core/agent-loop/tests/interception.spec.ts`

```ts
// 注册方式（插件内）
ctx.on('tools/pre-execute', async (exec, next) => { ... return decision })
ctx.on('tools/post-execute', async (exec, result, next) => { ... return decision })

// exec 字段：exec.name（工具名）、exec.arguments（已解析参数，类型 unknown）
// pre-execute 决策：
type PreToolDecision =
  | { kind: 'allow' }
  | { kind: 'deny'; reason: string }   // deny → 跳过执行，reason 作为
  | { kind: 'ask'; reason?: string }   //   isError 工具结果被模型看到（有测试背书）
// post-execute：result.isError === false 时 result.value 是规范化输出
// （我们工具的输出形状是 { text, state, raw }，state.observation_state 即页面结构化状态）
```

**关键语义**：`deny` 会短路执行，`reason` 文本出现在模型看到的工具错误结果里——这正是"拦截 + 纠正指引"的通道，无需任何额外机制。

---

## 2. 改动清单（改哪里、怎么改、为什么）

### 2.1 新增 `src/buy-guard.js`（核心，约 120 行）

参考实现（可直接用，纯函数已抽出便于单测）：

```js
/**
 * Buy Guard：click[buy now] 前置拦截。
 * 只有"在商品详情页、按钮列表里有 buy now、已选规格"三者同时满足才放行，
 * 否则拒绝并把纠正指引返回给模型。页面状态来自每步工具返回的
 * observation_state（白名单字段，模型同样可见，无泄漏）。
 */

export const name = 'buy-guard'
export const inject = ['tools']

export function normalizeClickValue(value) {
  return String(value ?? '')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .trim().toLowerCase()
}

export function isBuyNow(value) {
  return normalizeClickValue(value) === 'buy now'
}

/** 纯函数：给定最新 observation_state，判定 buy now 是否放行。 */
export function checkBuyNow(state) {
  if (!state || typeof state !== 'object') {
    return {
      allowed: false, rule: 'R0',
      feedback: '[购买被拦截] 尚未进入任何商品页面。请先用 search 搜索，再点击商品进入详情页。',
    }
  }
  const pageType = String(state.page_type ?? '')
  const actions = Array.isArray(state.actions) ? state.actions.map(normalizeClickValue) : []
  const selected = (state.selected_options && typeof state.selected_options === 'object')
    ? state.selected_options : {}
  const selectedEntries = Object.entries(selected)
  const stateDesc =
    `当前页面：${pageType || '未知'}；可点击按钮：${JSON.stringify(state.actions ?? [])}；` +
    `已选规格：${selectedEntries.length ? JSON.stringify(selected) : '无'}`

  if (pageType !== 'product_detail') {
    const fix = pageType === 'information_subpage'
      ? '请先点击 "< prev" 返回商品详情页'
      : (pageType.startsWith('search') || actions.includes('back to search'))
        ? '请先点击目标商品进入商品详情页'
        : '请先导航到商品详情页'
    return {
      allowed: false, rule: 'R1',
      feedback: `[购买被拦截] 当前不在商品详情页，无法购买。${stateDesc}。${fix}，再点击 Buy Now。`,
    }
  }
  if (!actions.includes('buy now')) {
    return {
      allowed: false, rule: 'R2',
      feedback: `[购买被拦截] 当前页面的可点击按钮中没有 Buy Now。${stateDesc}。`,
    }
  }
  if (selectedEntries.length === 0) {
    return {
      allowed: false, rule: 'R3',
      feedback: `[购买被拦截] 尚未选择任何商品规格，不能购买。${stateDesc}。请先在商品详情页点击目标规格选项，再点击 Buy Now。`,
    }
  }
  return { allowed: true, rule: null, feedback: '' }
}

export function apply(ctx) {
  // 一个 dsh 进程 = 一个任务一个会话，模块级缓存即可
  let lastState = null

  // 记录：每次环境工具成功返回后，缓存最新页面状态
  ctx.on('tools/post-execute', async (exec, result, next) => {
    try {
      if (!result.isError && (exec.name === 'search' || exec.name === 'click' || exec.name === 'finish')) {
        const os = result.value?.state?.observation_state
        if (os && typeof os === 'object') lastState = os
      }
    } catch { /* 观察者永不破坏主链路 */ }
    return next()
  })

  // 拦截：click[buy now] 前置检查
  ctx.on('tools/pre-execute', async (exec, next) => {
    if (exec.name !== 'click') return next()
    const args = (exec.arguments && typeof exec.arguments === 'object') ? exec.arguments : {}
    if (!isBuyNow(args.value)) return next()
    const verdict = checkBuyNow(lastState)
    if (!verdict.allowed) {
      console.error(`[buy-guard] denied rule=${verdict.rule} page=${lastState?.page_type ?? 'none'}`)
      return { kind: 'deny', reason: verdict.feedback }
    }
    return next()
  })
}
```

**为什么**：R1（必须在商品详情页）有 213:0 的数据支撑，预期零误拦；R2 是 R1 的双保险；R3 是环境本身的规则（不选规格不能买）。反馈文本全部来自白名单字段（模型同样能看到的信息），无泄漏。

### 2.2 改 `package.json`：加一个 export

```json
"exports": {
  ".": "./src/shop-tools.js",
  "./buy-guard": "./src/buy-guard.js"
}
```

**为什么**：让 profile 的 patch 能用 `@shopping-longhorizon/shop-tools/buy-guard` 引到插件。**只加声明，不插入任何 profile——基线不受影响。**

### 2.3 新增 `harness/h1/package.json`

复制 `harness/h0/package.json`，仅改 name：

```json
{
  "name": "dsh-profile-h1",
  "private": true,
  "dependencies": {},
  "dsh": {
    "profile": {
      "bundles": [
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-headless",
        "@shopping-longhorizon/shop-tools"
      ]
    }
  }
}
```

### 2.4 新增 `harness/h1/cordis.patch.yml`

内容 = `harness/h0/cordis.patch.yml` **原样全文**，末尾追加：

```yaml
# ── h1 新增：购买守卫 ──────────────────────────────
- insert:
    - id: buy-guard
      name: '@shopping-longhorizon/shop-tools/buy-guard'
```

**为什么**：h1 与 h0 的唯一差异就是这一个插件——A/B 归因严格干净。

### 2.5 改 `scripts/setup_harness.sh`：支持任意 profile 名

把两处硬编码 `harness/h0/` 改为 `harness/$PROFILE_NAME/`，并加存在性检查：

```bash
PROFILE_SRC="$ROOT/harness/$PROFILE_NAME"
[[ -d "$PROFILE_SRC" ]] || { echo "错误: $PROFILE_SRC 不存在" >&2; exit 1; }
...
cp "$PROFILE_SRC/cordis.patch.yml" "$PROFILE_DIR/cordis.patch.yml"
cp "$PROFILE_SRC/package.json" "$PROFILE_DIR/package.json"
```

（即把原脚本中 `cp "$ROOT/harness/h0/cordis.patch.yml" ...` 和 `cp "$ROOT/harness/h0/package.json" ...` 两行替换。）

### 2.6 `scripts/run_batch.sh`：无需改动

已支持 `DSH_PROFILE` 覆盖（`${DSH_PROFILE:-h0}`），确认一下即可。

---

## 3. 验收（按顺序做，全过才算完成）

### A1 规则单测（确定性，不依赖 dsh/模型）

新建 `scripts/test_buy_guard.mjs`，import `checkBuyNow`/`isBuyNow`，断言以下场景：

| # | 输入状态 | 期望 |
|---|---|---|
| 1 | `null` | deny R0 |
| 2 | `page_type=information_subpage, actions=["back to search","< prev"]` | deny R1，feedback 含 `< prev` |
| 3 | `page_type=search_results, actions=["next >","<asin>"]` | deny R1，feedback 含 "点击目标商品" |
| 4 | `page_type=product_detail, actions=["description","buy now"], selected_options={}` | deny R3 |
| 5 | `page_type=product_detail, actions=["description"]（无 buy now）, selected_options={"颜色":"A"}` | deny R2 |
| 6 | `page_type=product_detail, actions=["buy now"], selected_options={"颜色分类":"X"}` | allow |
| 7 | `isBuyNow("Buy Now") / ("buy now ") / ("buy now!")` | true / true / false |

运行：`node scripts/test_buy_guard.mjs`（用 `node:assert`，失败退出码非 0）。

### A2 安装与加载验证

```bash
bash scripts/setup_harness.sh h1
cd deepseek-harness
DSH_HOME="$PWD/../.dsh-home" pnpm dsh --profile h1 --dump-config | grep buy-guard   # 必须命中
DSH_HOME="$PWD/../.dsh-home" pnpm dsh --profile h0 --dump-config | grep buy-guard   # 必须为空（h0 未被污染）
```

### A3 端到端（失败场景复现）

```bash
# 环境先起好：bash scripts/start_environment.sh（+ M+P 需要 start_shopper.sh）
# 挑一条历史上死于"子页面点购买"的任务：idx 10/11/12/23/26/27 任一
DSH_PROFILE=h1 SHOPSIM_IF_PERSONA=1 bash scripts/run_batch.sh 1 11
```

检查最新 `runs/<时间戳>/traces/11.model_trace.json`：
- 若模型再次尝试子页面购买 → 该步 observation 里应出现 `[购买被拦截]` 文本，且任务**继续**而非静默失败；
- 若模型这次直接买对（行为有随机性），A3 记为"无拦截机会"，不算失败——A1+A2 已证明机制正确。

### A4 零回归

```bash
DSH_PROFILE=h0 SHOPSIM_IF_PERSONA=1 bash scripts/run_batch.sh 1 2
```
（idx 2 历史上 11 步买中。）确认行为与历史一致、无 `[购买被拦截]` 出现。

### A5 无泄漏扫描

对 A3/A4 的 model_trace 全文扫描：不得出现 `.shopper_facts`、`instruction_full`、`goal_options`、gold asin 等字段名/值。拦截反馈只能含 `page_type/actions/selected_options` 的内容。

---

## 4. 提交要求

全部验收通过后，在 `main_buy_guard` 分支提交（可分两次）：

```
feat: buy-guard 前置拦截插件 + h1 profile
test: buy-guard 规则单测
```

## 5. 明确不做（防止范围蔓延）

- ❌ 价格/预算校验、需求版本绑定、两阶段购买协议（后续版本）
- ❌ 状态投影注入模型上下文（下一个机制）
- ❌ 修改任何提示词、任何现有文件的行为（除 2.2/2.5 两处声明性改动）
- ❌ 拦截 `search`/`finish`/`ask_shopper` 的任何行为
- ❌ 动 `runs/` 历史数据和 `.shopper_facts/`

## 6. 风险与备选

- **风险**：`post-execute` 的 `result.value` 形状与预期不符（取不到 `state.observation_state`）。
  **诊断**：在 post-execute 里临时 `console.error(JSON.stringify(Object.keys(result)))` 跑一条任务看结构，按实际字段调整读取路径。
- **风险**：模型不理解拦截反馈，反复撞墙。
  **对策**：反馈文本已给出精确按钮名；若 A3 观察到连续 3 次同类拦截，记录为后续"状态投影"机制的动机案例，不在本单处理。
- **备选**（仅当 pre-execute API 实际不可用时）：在 `src/shop-tools.js` 的 click 工具内部做前置检查——**不推荐**，会污染基线插件，启用前必须说明理由。
