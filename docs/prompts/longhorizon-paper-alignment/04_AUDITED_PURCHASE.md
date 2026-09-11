# 阶段4：Audited Completion 与两阶段购买授权

## 任务

解决购物环境中 `Buy Now` 不可逆终止、从而绕过 Auditor 的问题。实现“先核验购买意图，
再授权最终购买”。

## 前置

阶段1-3完成，Auditor已能独立读取当前公开商品、规格和价格状态。

## 两阶段协议

```text
Round A: 搜索/选择商品与规格
  -> Executor report purchase_intent
  -> Auditor 独立检查商品、规格、价格、包装数量、有效用户需求
  -> clean + complete audit
  -> reducer 写入 purchase_authorization

Round B: fresh Executor 只执行已授权的 Buy Now
  -> pre-execute guard 比对当前状态与 authorization
  -> 购买
  -> Auditor/公开 receipt 检查最终结果
```

`purchase_authorization` 至少包含 product id、selected options、公开价格、包装数量、需求版本、
audit id 和失效条件。

## 硬规则

- 没有有效 authorization 时拒绝 Buy Now。
- 页面商品或 selected options 与 authorization 不一致时拒绝。
- 新 shopper 回复、页面变化或状态版本变化使旧 authorization 失效。
- 工具不支持设置购买数量时，不得假设可以买多件。
- “1支×100=100元”不能证明实际将购买数量设为100。
- 最终 done 必须由 clean completion audit 支持，不能由 Executor 文本声明。
- 购买回执只提供公开成交事实，不把 reward/gold 暴露给在线角色。

## 重点回归

- task263：选择“1支”时必须禁止把一次 Buy Now 当100根购买。
- task916：用户未接受168元时不得授权购买。
- task204：公开商品与规格满足时，Auditor应能生成授权，不能无故阻塞。
- task585：分别展示环境隐藏目标和公开用户需求结论，不混用。

## 测试

- 未授权 Buy Now 被拒绝。
- clean audit 后匹配状态可购买。
- 规格/价格/需求改变后授权失效。
- 数量能力缺失时拒绝多件推断。
- Buy Now之后没有新的购物动作。
- completion只有 clean audit 才能写入。

## 完成条件

上述4个真实 case 小批次通过，且没有放宽 Rubric、环境 reward 或 Judge 规则。

