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
