// Buy Guard 规则单测（纯函数，不依赖 dsh/模型）。
// 运行：node scripts/test_buy_guard.mjs
import assert from 'node:assert/strict'
import { checkBuyNow, isBuyNow } from '../src/buy-guard.js'

// 1. null → R0
{
  const v = checkBuyNow(null)
  assert.equal(v.allowed, false)
  assert.equal(v.rule, 'R0')
}

// 2. information_subpage → R1，feedback 含 "< prev"
{
  const v = checkBuyNow({ page_type: 'information_subpage', actions: ['back to search', '< prev'] })
  assert.equal(v.allowed, false)
  assert.equal(v.rule, 'R1')
  assert.ok(v.feedback.includes('< prev'))
}

// 3. search_results → R1，feedback 含 "点击目标商品"
{
  const v = checkBuyNow({ page_type: 'search_results', actions: ['next >', '<asin>'] })
  assert.equal(v.allowed, false)
  assert.equal(v.rule, 'R1')
  assert.ok(v.feedback.includes('点击目标商品'))
}

// 4. product_detail + buy now，但未选规格 → R3
{
  const v = checkBuyNow({ page_type: 'product_detail', actions: ['description', 'buy now'], selected_options: {} })
  assert.equal(v.allowed, false)
  assert.equal(v.rule, 'R3')
}

// 5. product_detail，无 buy now 按钮，但已选规格 → R2
{
  const v = checkBuyNow({ page_type: 'product_detail', actions: ['description'], selected_options: { '颜色': 'A' } })
  assert.equal(v.allowed, false)
  assert.equal(v.rule, 'R2')
}

// 6. product_detail + buy now + 已选规格 → allow
{
  const v = checkBuyNow({ page_type: 'product_detail', actions: ['buy now'], selected_options: { '颜色分类': 'X' } })
  assert.equal(v.allowed, true)
  assert.equal(v.rule, null)
}

// 7. isBuyNow 归一化
{
  assert.equal(isBuyNow('Buy Now'), true)
  assert.equal(isBuyNow('buy now '), true)
  assert.equal(isBuyNow('buy now!'), false)
}

console.log('test_buy_guard.mjs: all assertions passed')
