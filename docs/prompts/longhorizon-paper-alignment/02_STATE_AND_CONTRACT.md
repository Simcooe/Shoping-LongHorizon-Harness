# 阶段2：结构化 Task State、Contract 与逐条证据更新

## 前置与范围

阶段1通过。阅读论文2.1、2.2、2.4及现有 `src/mea-loop.js`。
本阶段只实现schema、校验器、reducer和Manager输入输出契约；真实Manager接线由阶段5负责。
建议：`src/mea-v4/state.js`、`schema.js`，纯函数测试。

## 状态结构

保留原始任务及公开画像原文、真实shopper回复。requirement由公开任务/回复派生；
不能在线读取离线Rubric来初始化状态。

requirements、artifacts、facts记录至少包含：

- 稳定id、类型、内容、来源；
- status：pending/completed/blocked/untrusted；
- requirement_version、适用对象scope（如商品ID、变体）；
- evidence_refs及dependencies；
- 对已修改/撤销需求的明确有效性标记；保留旧版本，而不是删除历史。

Audit Report包含id、contract_id、status、integrity、逐条finding、evidence、
remaining_gaps及建议更新。逐条finding能定位它核验了哪个record/criterion。
Audit只提出更新，Manager选择如何纳入状态，reducer验证并应用；不能让Manager凭空改写事实。

## 局部核验与失效

- Executor report是待核验声明，不能直接推进完成。
- audit.status=incomplete且integrity=clean时，允许使用其中明确支持某项事实的finding
  更新该项。例如颜色确认、尺寸未知：保存颜色，尺寸保持pending。
- suspect/violation audit不用于新增completed记录；保留问题和证据以便后续重新核验。
- audit.status=complete也不能自动把所有任务要求标成completed。
- evidence必须指向真实audit/tool observation；历史audit引用可复用，不能因为跨audit
  就一律拒绝，但须解析引用并检查范围、有效性及依赖。
- 商品A的价格事实继续属于A；换到B后不得作为B的价格或最终需求满足证据。
- 更换规格或需求版本后，重新检查依赖它的完成判断，必要时回到pending/untrusted。
- 已审计的历史事实可以保留，是否适用于当前决定另行判断。

## Contract与Manager契约

Contract包含goal、acceptance_criteria、boundary_constraints、dependencies、
relevant_state_ids、relevant_audit_ids、角色工具能力和独立执行预算。
区分角色工具权限与Manager建议工具；购物操作细分到参数时须用结构化规则，
不能认为allowed_tools=["click"]已经表达了“允许选规格但禁止购买”。

Manager输入为原始任务、当前状态、累积audit及真实用户回复；不得直接读环境或Executor历史。
Manager输出包含state_updates、decision（execute/done/blocked/ask）、reason；
execute带contract，ask带一个具体question，其他分支不携带执行contract。
阶段5的Manager提示词和调用必须遵守这里的同一schema。

任务done要求当前有效要求满足，完成证据仍有效，最终审计complete且clean，
无未处理integrity violation。准备好候选与实际购买完成分别建模，不能提前宣告成功。

## 验收与交接

测试局部incomplete/clean推进、无证据拒绝、过期引用拒绝、跨audit合法引用、
换商品/规格失效、用户撤销/修改需求、Manager非法更新以及JSON round-trip。
冻结schema和示例输入输出供阶段3-5使用。
