# 阶段4：独立只读Auditor与终局成交审计

## 前置与范围

阶段1-3通过。阅读论文2.4、ShopSimulator的pack_api.py、shop_agent.py、
structured_observation及terminal renderer。实现独立inspection和Auditor adapter。
新API需单独标记版本，旧reset/interact行为保持回归一致。

## 先定义可核验范围

只读接口必须能覆盖：

1. 当前公开页面、当前选择和可见操作；
2. 核验所需的公开商品信息：若允许查看已访问商品/子页，应通过不影响Executor的只读视图；
   定义可访问对象，禁止遍历隐藏商品库或读取未公开属性；
3. 终局后的脱敏成交事实：商品、选项、实际金额、数量/包装（仅实际记录存在时）。

不能只返回“Episode finished”再让Auditor猜测成交，也不能把整个structured_observation
默认当公开数据。先给每个字段建立来源映射，确认它对应公共页面或实际成交记录。
不从reward_detail或目标goal推导是否买对；缺少数量、价格等字段时返回unknown，
不能按单价乘用户期望数量来虚构订单。

终局收据是购物适配新增的在线观察能力，必须记录能力差异及版本；
这不改变环境奖励，也不把新增审计证据自动喂给离线Judge。

## 独立读取与只读边界

通过有界inspect请求从env直接读取；禁止修改page/options/progress/reward/done/step。
不通过普通click/search实现Auditor导航；可以使用独立只读视图检查公开信息。
inspect要求有效environment session/lease，不得通过缺env_idx的路径自动租用新slot。
在Executor停步后审计；并发请求串行化，snapshot带版本及证据来源。

只读性测试比较任务相关环境状态（页面、选择、订单、计数、lease等）；
审计日志自身增长不算违规。终局前后都测试无任务状态修改。
若检测到任务状态修改，记录integrity violation，该audit不能支持completed。

## Auditor输入输出

输入：原始任务、Task State、contract、引用的prior audits、Executor report。
不接收Executor原始轨迹或内部推理；report用于定位，不能证明结果。
Auditor调用独立只读工具获取新证据；允许引用仍有效的prior audit证据，
新环境结论不能只由report或Manager摘要支持。

输出沿用阶段2 schema：complete/incomplete/blocked、clean/suspect/violation、
逐条findings/evidence、remaining_gaps、建议state_updates。
证据id由runtime登记，绑定scope/snapshot，不由LLM自行伪造。
每次Auditor独立context，有预算、session和usage日志。

## 验收与交接

- 谎报规格时按独立环境快照识别差异。
- Executor结束后仍能独立核对实际订单，缺字段判unknown。
- 只读调用前后任务相关状态不变；越权/无lease/错误session被拒绝。
- 公开字段与gold/reward/私有目标隔离，无法读取Executor原始历史。
- incomplete+clean的部分finding可供reducer使用，失效的历史finding不被复用。
- 用mock订单和隔离环境验证购买后inspect；本阶段不实现购买前门禁。
