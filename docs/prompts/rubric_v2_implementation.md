# 可直接交给实施会话的提示词：多轮需求、rubric v2、Judge与report完整实现

请在仓库`/Users/ywwl/shopping-longhorizon-harness/Shoping-Longhorizon-Harness`完成以下完整实现和本地测试。用户目标是构建支持澄清、需求变更和个性化购物的长程Agent评测，而不仅是初始Query的静态评分。请直接写代码、验证并交付可执行CLI，不要只提交设计文档或未接线的辅助模块。

先读`reports/h0-0905-1446/evaluation_repair_plan.md`、`repair_implementation_review.md`、`docs/PROTOCOL_V2.md`和`docs/prompts/repair_followup.md`。基础补漏应先完成；检查实际代码与测试，已有修复沿用，仍阻塞本任务的基础错误先补齐。保留用户现有修改，不回滚。不得修改h1策略、泄漏隐藏答案、按case ID硬编码评分、猜SKU价格或覆盖任何旧benchmark/traces/rubrics/judgments/reports。不启动在线Agent或LLM全批次；使用确定性fixtures/mock响应验证，并提供之后真实生成和评测的命令。

## 一、交付范围与整体链路

必须贯通两种运行方式，使用同一套事件语义和约束编译器：

1. 新任务在线：初始需求 → Shopper的可见回复与结构化事件 → 同会话需求状态同步 → 环境购买/放弃判定 → 带事件版本的trace → rubric v2 → Judge → 四面板report。
2. 历史任务离线：保留原始trace，只从初始公开输入和历史ask_shopper问答重建事件 → 按原事件顺序编译v2需求时间线 → 新目录下生成judgments/report。必须标注retrospective（事后重建），不能声称当时环境已采用这些需求或Agent已在新协议下重跑。

在线账本及确定性环境裁判属于可信服务；模型可见rubric/过程Judge只能得到当时公开的信息。LLM负责语义提取与证据判断，代码负责schema验证、版本、事件生效、范围、冲突、分母及输出一致性，不能让report再猜一遍对话。

## 二、分清三种需求视角

- 初始公开约束：从instruction_simple/query中提取。现有正确v1条目可以沿用，输出到独立v2文件并保留来源/ID映射，不能只改version字段就当完成升级。画像是偏好上下文，不能自动升级为当前硬约束。
- Agent已知有效约束：初始公开约束加截至该动作已收到的澄清/修改/接受/拒绝事件。过程评分用这一视角，不回溯惩罚尚未获知的信息。
- 环境真实有效目标：原有完整隐藏需求仍存在，澄清只增加Agent知情范围；只有可信用户修改才改变目标。隐藏需求不得因没有被问到而从环境评分消失，也不能直接塞进Agent可见rubric。

必须区分reveal（揭示既有需求）、add（用户新增要求）、modify/supersede（替换）、conditional_accept（特定报价条件接受）、reject（拒绝候选/报价）、revoke（撤销）。具体枚举可复用现有模块，语义必须完整。拒绝与报价许可属于事件/决策规则，不应每出现一句话就重复增加一条普通属性约束。

## 三、可追溯schema和事件顺序

先定义并实现统一schema，可按仓库风格调整字段名，同时提供文档与验证器。至少包含：

```json
{
  "schema_version": "明确的新版本",
  "benchmark_id": "...",
  "run_id": "...",
  "task_id": "...",
  "trial_id": "...",
  "base_rubric_hash": "...",
  "trace_hash": "...",
  "event_source": "runtime或retrospective",
  "constraints": [{
    "id": "稳定约束ID",
    "requirement_key": "语义键，如budget或waterproof",
    "description": "...",
    "hardness": "hard或soft",
    "source": "initial_query或shopper_clarification等",
    "source_event_id": "...",
    "source_quote": "原话可定位片段",
    "visible_from_event": "...",
    "effective_from_event": "...",
    "effective_until_event": null,
    "supersedes": null,
    "lifecycle_status": "active/superseded/revoked",
    "scope": {},
    "conditions": []
  }],
  "requirement_events": [],
  "requirement_versions": []
}
```

事件有独立event_id、会话身份、expected_version/result_version、原值/新值、source_reply及内容指纹、稳定动作顺序。购买/放弃记录绑定requirement_version和决策event_id。scope要能精确约束报价涉及的候选、规格、数量、金额与货币等；不能把只针对168元商品的接受推广成任意商品预算提高。未明确的条件保留pending/unknown，不能擅自通过。

`step`编号可能重复，不可作为唯一ID。新日志保留工具call/result的可关联ID并新增唯一事件/动作标识；旧轨迹使用确定性映射（run/task/事件顺序等），输出映射供追溯。缺raw的工具结果不得导致model/raw按数组位置错配；并发工具结果须按调用ID匹配，不能用单个pending变量覆盖。确实无法唯一映射时显式报缺证据，不把同编号第一条当答案。

在线事件与历史推断分开标记，历史提取不得读取gold ASIN、raw reward、hidden full instruction来补充“用户说过”的要求。允许受控读取终止边界和事件元数据完成机械分段，但不得把后台结果发送给语义Judge。

## 四、Shopper与环境在线接线

修改scripts/shopper_simulator.py、src/shop-tools.js、必要的服务路由/runner初始化和环境goal读取，使账本真正被调用：

- Shopper维护完整当前需求，并同时产出可见reply和对应结构化事件；允许受schema约束的语义提取，但同一回复不能在环境和rubric侧被两个互不一致的提取器各解释一次。不确定提取进入待确认，不能告诉Agent已变更却没有同步。
- 按run/task/trial隔离，不能继续只用task_idx作全局session。reset/start/end与重试路由使用同一身份；复用slot不串话。必要时持久化事件以支持重启/重试，不依赖不可恢复的全局内存编号。
- 可信同步接口校验会话归属、事件版本和内容；Agent工具参数不允许任意改后台goal。支持同事件同内容幂等重试；异内容冲突、旧版本乱序、同步失败给可诊断状态，阻止依赖未确认新需求的购买。
- 条件接受由公共商品证据核验条件并限定报价范围；真实修改更新环境有效目标。重算已打开候选的可接受集合，移除被拒绝或被新需求排除的旧候选，不能只向known_valid_asins追加。
- 终止后对话不能改变已锁定结果；原15例中硬停止后的Shopper回复必须在历史v2解释里标为post_terminal，不计入终止前有效约束。可另存事后解释，不丢原文。
- 环境输出保留initial_static_result（明确基准口径）和active_requirement_result（有足够信息才计算），绑定版本及证据。历史缺SKU/条件证据则unknown，不为了生成动态分猜数据。静态预算“不通过”和动态报价“被许可”可以同时展示，但不能重复算成两个最终有效要求。

## 五、rubric生成：基础清单与每条轨迹的动态清单分开

现有gen_rubric.py是initial_query_only_v1。新增明确的v2生成/验证入口，可扩展原CLI或新增脚本，但必须实际支持运行，不只写转换说明。

- benchmark级base rubric保持与Agent轨迹无关；run/task/trial级动态rubric从base+该轨迹事件编译。动态rubric不写回benchmarks/.../rubrics覆盖所有Agent共享文件。
- 同义澄清、重复确认合并到同一语义要求，保留来源事件，避免靠多问几次增加分母；不同含义不得因文字相近误合并。
- 维护完整时间线及每一决策时刻的有效集合。superseded/revoked是生命周期，不能偷换成Judge的not_applicable；未完成条件接受不消除初始要求。单笔报价许可不必把全局预算约束标为superseded。
- final约束集合取实际决策/选定终止协议的时刻，不能简单取全文件最后回复。没有有效购买/明确最终候选时按终止时已知要求展示，并明确最终执行未完成。
- LLM提取有schema校验、有限重试和显式失败记录，禁止失败后静默用空事件集或全部satisfied。程序可测试dry-run/fixture模式；真实LLM模式可配置，复用已有凭据读取方式而不输出密钥。
- 新输出manifest记录base/task集合、输入hash、协议、模板/提取器/模型版本、生成时间和失败清单。缓存只有输入及所有影响语义的版本一致时复用；支持resume，不能只看frozen=true就跳过陈旧结果。

## 六、Judge v2：四态和七维保留，但时间与证据明确

修改eval/judge.py，读取v2 rubric时间线、Agent可见观察、声明的终止协议及必要的公开最终回复；保留显式v1兼容模式，不能把旧judgment冒充v2。

- 每步过程质量按当时已知约束评价；最终决策按当时有效需求及报价许可评价；不因未来需求批评过去，也不继续按被撤销要求扣最终分。
- 保留satisfied/violated/unknown/not_applicable四态，新增唯一事件引用与requirement_version，并验证引用真实存在且时间合法。引用不仅要“某个step存在”，还要明确是哪次动作和哪段可见证据。判定必须覆盖要求，无漏项、重复项或未知ID。
- 区分“某个浏览候选有属性证据”和“最终决定满足要求”。例如case6选到33元红把手但没有购买：环境仍失败，候选可有匹配证据，不能把它变成购买成功。建议final_requirement_verdicts与candidate_evidence分开；无有效最终决定时最终满足状态用unknown，已有明确违规操作可单独记录，不因未购买而抹掉。
- 七维仍为澄清、信息保持、搜索、候选利用、证据核验、决策、终止效率，每项0/1/2；增加每维简短理由和事件证据便于审计。没有ask不自动扣分，也不能自动证明信息保持优秀；按可见任务需要解释评分，报告保留适用上下文。
- 接入最终assistant可见text用于判断“声称买好了”等行为，声明本身不能证明成交。禁止读取/输出内部推理当作证据。Judge不读取后台reward/gold匹配，不把Episode finished作为成功证据。
- 解决固定1800字符头尾截断丢价格/规格问题：允许完整观察、可验证的按事件分块或保留关键原文及来源位置的证据预算；不得丢掉关键字段后假装输入完整。输出evidence_truncated/coverage等状态，缺关键证据判unknown。
- 对旧四态结果重新评判需要真实LLM调用，代码交付阶段用mock证明链路；不得把mock判断写到正式evaluations冒充真实模型结果。

## 七、report v2：四面板保留，来源、分母、版本可解释

修改eval/report.py和必要的evaluate输出衔接，report只合并统计，不做自然语言语义提取。

1. 环境面板：当前选定协议结果、first_terminal/legacy_canonical_terminal对照、原静态/动态结果的可用性、requirement_version、reward有效性/价格数据缺口。历史动态结果不能核实就unavailable/unknown，不能填0或伪造成功。
2. Rubric面板：初始公开需求、对话新增/澄清、有效修改、条件报价许可、撤销历史可追溯；最终指标仅统计决策时有效约束，排除superseded/revoked而保留时间线。条件许可单独展示，不重复作为“多一个满足项”。显示来源quote、事件、版本、四态、理由。
3. 七维面板：每维均值/分布、理由可追溯；按是否澄清、任务类别/复杂度等分组时给样本数。不同Agent的动态约束数不一致，初始与动态指标分开，不用一条混合总满足率声称公平提升。
4. 行为面板：真实决策/购买状态、首次停止与事后动作、非法点击、漏选、同步失败、无进展等已证实行为；明确Agent错误、环境数据不足和协议错误的来源，避免把reward不可核验等同买错。异常统计标明任务数/动作数。

给出精确定义的分子、分母和适用集。四态N/A、生命周期排除、unknown、unavailable、缺失文件不得混淆；零分母返回null及样本数0。初始原口径可作为单独历史对照；若初始约束已被替换，不能同时把它列入最终有效集合。当前动态有效约束的满足率只能在报告了约束量/分组的前提下解释，不与其他run的初始满足率混为同指标。

按benchmark/run/task/trial/event及输入hash校验关联；校验文件内部task_id、rubric/judge/schema/终止协议版本、模型配置、约束ID精确覆盖、事件引用和需求版本。缺失/混版默认报错；显式允许部分结果时必须展示覆盖率与实际分母，不能静默忽略缺失。不同输出目录隔离v1/v2；当前旧文件不得覆盖。附可机器读取JSON、summary、逐任务详情和易读Markdown。

## 八、必须通过的验收场景

- 初始任务完整且无ask：v2基础需求不凭空扩充，不自动扣澄清分。
- 1092：用户明确必须调光后新增硬要求，后续选择必须考虑；不能从商品“+60%”猜出可调光能力。首次透露前不以未知要求追罚。
- 916：168元、指定候选/规格、香港进口中药版条件确认后许可该报价；170元、别的规格、条件未知、用户拒绝、撤回许可不获同样放宽。原静态预算结果保留；在线Shopper→路由→环境→trace→rubric→Judge→report链路使用同版本，不能只测账本列表。
- 343/594：新要求排除原候选时重算候选集合，旧可接受计数不能强迫购买；普通“绿色”不能自动证明偏黄草绿。
- 196：不超过20块仍是20元，拒绝30元不能因预算解析丢失被判错。
- 263：要求100根而实际50支继续是明确数量问题，不能靠宽松规则洗掉。
- 6：候选匹配、没有购买、循环终止可以同时表达；最终需求满足/候选证据/环境成功不互相替代。
- 原15个放弃：第一次硬停止之后的澄清/finish不改变v2终局前有效需求，旧口径仍可显式重现。
- 重复step、多工具call/result、缺raw、证据截断、同事件重复/异内容重放、跨run隔离、同步失败重试、乱序事件、条件接受后拒绝/撤销、嵌套快照修改均有回归。
- 重复澄清不增加分母；替换不双重扣分；scope许可不覆盖其他报价；N/A/unknown/无样本/缺文件显示明确。覆盖schema校验和manifest缓存失效。

测试应使用可控的Shopper/Judge mock、临时数据目录和最小商品集，包含实际组件串联的集成测试，不修改真实记录或启动全量线上模型。测试fixtures可以使用案例语义，但实现必须通用；模拟商品价格必须标注fixture，不能回填正式SKU源。

## 九、执行顺序与完成标准

先定义schema和依赖协议；完成事件账本/路由/环境投影；完成在线导出与历史事件重建；完成动态rubric生成验证；完成Judge输入输出与证据规则；完成report/manifest；运行相关与集成测试；更新CLI文档和迁移说明。

提供可验证的命令，分别覆盖：生成base rubric、历史轨迹重建动态rubric、验证输出、运行Judge v2、合并report v2、在线新run的同协议h0/h1评测。确认--help及路径，明确工作目录和独立输出位置。不执行真实LLM批次，但实现真实调用路径并说明何处需要API服务。缺SKU数据不妨碍代码交付，列为数据限制。

最终交付修改文件列表、字段契约、实际测试结果、至少916/1092/6的fixture输出和时间线、旧v1兼容验证、哪些历史数据只能事后重建、哪些真实调用尚未执行、运行命令和剩余限制。不能仅以“基座已完成，在线接线以后再做”结束，也不能把fixture分数称作真实新成绩。
