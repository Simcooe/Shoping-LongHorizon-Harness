# 环境与评测协议升级说明（Protocol v2）

依据 `reports/h0-0905-1446/evaluation_repair_plan.md` 与
`reports/h0-0905-1446/repair_implementation_review.md` 实施。本文件定义本次
协议升级的版本、字段约定、新旧口径对照、剩余数据问题与重跑命令。

本次只修协议与环境代码，**不修改** 冻结 benchmark、历史 traces、rubrics、
judgments 与历史报告；不修改 h1 策略；不猜测 SKU 价格；不按 case 硬编码；
不启动完整在线模型批次重跑。动态需求在线接线与 rubric/Judge/report 全量
语义升级由 `docs/prompts/rubric_v2_implementation.md` 承接。

## 1. 变更清单

| 方案步骤 | 文件 | 变更 |
|---|---|---|
| 1 统一终止协议 | `environments/.../envs/web_agent_text_env.py` | `terminal-lock-v1`：done=true 后锁定环境，动作不再路由，终局不可改写；终局后动作写入 `post_terminal_log`，finish 理由写入 `post_terminal_explanations`。第一次真实 done 冻结完整不可变终局快照 `terminal_snapshot`（reward/reward_detail/termination_reason/reward_valid/purchase + 协议信息），锁定响应不再返回 `purchase={}` 或丢失终止原因 |
| 1 统一终止协议 | `eval/trace_utils.py` | 新增 `first_terminal_step()` / `select_terminal()` 与协议常量；`canonical_terminal_step()` 保留为历史口径 |
| 1 统一终止协议 | `eval/evaluate.py` | `--terminal-protocol {v1,v2}`（默认 v2）；每任务输出 `first_terminal` 与 `legacy_canonical_terminal` 双口径；summary 附 `legacy_outcome_classes` / `input_fingerprint`；`--force-overwrite` 显式开关，默认拒绝覆盖已有结果 |
| 1 统一终止协议 | `scripts/export_trace.py` | `--terminal-protocol {v1,v2}`（默认 v2）：摘要按第一次真实 done 构造；`terminal-protocol-v1` 显式保留旧的「最后 done」口径；`terminal_protocol` 写入两个 trace |
| 2 预算编译 | `environments/.../engine/constraints.py` | `budget-compile-v2`：`compile_budget()` 支持 元/块/块钱、售价/价位、别超/不要超过/低于、左右/上下/来/出头/多（冻结容差 ×1.1 只应用一次）、常见中文数字（两百/一千三/三十元/五千元/五十多块等可确定性解释形式）；区分 `declared / undeclared / parse_failed`（parse_failed 带明确 `reason`，不猜值）；显式区间保留上下界；开放区间/下限表达（如 4k+）不冒充硬上限 |
| 2 预算编译 | `environments/.../engine/goal.py` | goal 编译写入 `budget_status` / `budget_lower` / `budget_reason` / `budget_compile_version` |
| 2 预算编译 | `environments/.../engine/reward.py` | 预算门槛（budget-gate-v2）：显式区间上下界都参与判定（闭区间，`budget_range_v2`）；仅上限不凭空加下界（`variant_price_budget_v2`）；`parse_failed` → `unverifiable`（`budget_parse_failed_v1`），不冒充未声明；无 `budget_status` 的旧 goal 沿用 `variant_price_budget_v1` 等旧比较器名，兼容旧评分 |
| 3 SKU 与购买状态 | `environments/.../engine/variant_price.py` | `variant-price-v2`：重复组合价格冲突不取首条（`duplicate_combination_price_conflict` → unverifiable）；`variant_combinations_complete=true` 的可信完整组合表：表外组合不回退单轴价/起价（`combination_not_in_trusted_table`）；`purchase_readiness()` 校验轴、值、组合合法性，区分 `ready / missing_options / invalid_selection / invalid_combination / data_conflict`（用户漏选 / Agent 非法选择 / 商品数据缺陷分开标记） |
| 3 SKU 与购买状态 | `environments/.../envs/web_agent_text_env.py` | Buy Now 前置校验：漏选规格 → 模型可见 `missing_options` 提示（不提交订单、不加购买计数、不标 done，可补选）；非法值/表外组合 → 拦截反馈，不形成有效购买；动态反馈内容 HTML 转义（`inject_guard_notice`）；`done()` 成交价不再回填起价，起价另存 `display_base_price` + `price_source`；`item_page` 拆出 `_render_item_page` 供拦截后重渲染 |
| 3/4 出站字段 | `environments/.../shop_env/shop_agent.py` | interact 响应按明确 schema 投影 `locked / terminal_lock_version / post_terminal_action / missing_options / purchase_submitted / readiness_status / readiness_code`；`get_purchase_info` 保留 `price / display_base_price / price_source`；reset 返回 `protocol_versions`（budget/variant_price/terminal_lock/requirement 协议指纹，随 `runs/<run>/reset/` 落盘） |
| 4 动态需求基座 | `environments/.../engine/requirements.py`（新增） | `requirement-protocol-v1`：版本化需求账本。深拷贝隔离（输入/事件/快照均不可外部篡改）；同 ID 同内容幂等重试、同 ID 异内容报 `event_id_conflict`；`expected_version` 前版本校验；撤销后按确定性历史重放重算（撤销旧修改不覆盖较新有效修改）；拒绝使同作用范围旧接受失效；条件接受必须验证完整作用范围（asin+price）与来源回复；会话隔离。纯逻辑基座，在线接线为后续阶段 |
| 5 评测版本区分 | `scripts/precheck_benchmark.py`（新增） | 只读预检：目标 SKU / 类目 / 预算可编译性（含原因）/ 目标规格可选性 / 价格可核验性 |
| 5 评测版本区分 | `eval/report.py` | 统一报告保留并校验 deterministic 协议信息：`first_terminal / legacy_canonical_terminal` 进 Panel 1；`terminal_protocol / evaluator_version` 进 metadata；同一报告内混用协议报错 |
| 5 评测版本区分 | `configs/environment.json` | `variant_price_version` → `variant-price-v2`（`environment_version` 保持 v2.1，benchmark 冻结值不变） |

## 2. 版本与字段约定

- `terminal-protocol-v2`（eval/export 默认）：第一条 done=true 终局锁定；
  `terminal-protocol-v1`：历史 canonical 口径（自然终局覆盖硬停止 / 最后 done）。
  两种口径不得在同一报告内混用而不声明。
- `terminal-lock-v1`（环境运行时）：`locked / terminal_lock_version /
  post_terminal_action / post_terminal_log / post_terminal_explanations /
  terminal_snapshot`。
- `budget-compile-v2`：`{status, upper, lower, tolerance_applied, quote, reason}`；
  「左右/上下/来/出头/多」容差冻结为 ×1.1，仅对模糊表达应用一次；
  提取失败不得冒充未声明；开放区间不是硬上限。
- `budget-gate-v2`（reward）：`budget_range_v2`（闭区间）/
  `variant_price_budget_v2`（仅上限）/ `budget_parse_failed_v1` /
  `budget_not_declared_v1`；旧 goal 沿用 `*_v1` 名。
- `variant-price-v2`：可信完整组合（`variant_combinations_complete=true`）→
  表内唯一价格；表外组合 / 重复组合价格冲突 / 多轴无组合价 → 不可核验；
  未知成交价保留 null，`display_base_price` 只作展示起价。
- `purchase-state-v2`：`ready / missing_options / invalid_selection /
  invalid_combination / data_conflict`；漏选可补选；数据冲突不阻塞购买、
  交由不可核验流程（保持历史行为）。
- `requirement-protocol-v1`：见第 1 节第 4 行；基座测试通过**不等于**
  case 916 等已在线修复。
- `deterministic-evaluator-v2`：task_results 每行附 `evaluator_version /
  terminal_protocol / first_terminal / legacy_canonical_terminal`；summary 附
  `input_fingerprint`。

## 3. 新旧口径对照（h0-0905-1446）

| 口径 | task_success | success_gold | repeat_loop | max_steps | graceful_stop | early_abstain |
|---|---|---|---|---|---|---|
| v1（历史，保留） | 65 | 64 | 38 | 8 | 6 | 9 |
| v2（新协议） | 60 | 59 | 50 | 17 | 0 | 0 |

- 15 个放弃案例按新口径全部还原为第一次硬停止：**9 次 max_steps、6 次
  repeat_loop**（与复核方案一致）；后续 finish 仅作事后解释。
- task 1151：v1 = success_gold（idx 16 覆盖 idx 11 的 repeat_loop）；
  v2 = repeat_loop（第一次终局锁定）。
- v1 口径可用 `--terminal-protocol terminal-protocol-v1` 完整复现历史
  summary（已验证逐字段一致：成功 65，outcome/anomaly 分布全等）。
- 新口径确定性结果写入独立目录（如 `reports/h0-0905-1446-terminal-v2/`），
  历史目录未改动；对已有结果目录默认拒绝覆盖。
- 以上是对历史轨迹的**离线重解释**，不代表新环境重新跑过。

## 4. 剩余数据问题（预检输出，见 `reports/benchmark-precheck/shopping-final-v1.json`）

1. **多轴无组合价（价格不可核验，9 个任务）**：51、190、356、569、779、933、
   935、1240、1336（通用规则扫描结果；旧报告 10 例中的 846/1034 属「漏选
   规格」类，现由 missing_options 前置检查覆盖，859 的轴冲突走不可核验流程）。
   本地商品源缺可信完整组合价，**不得猜测补价**：需从可信原始 SKU 来源补
   数据；补不到则保留不可核验，或在下一次预检阶段隔离并说明，不能静默剔除。
2. **预算仍无法可靠编译（2 个任务，parse_failed）**：820（「价格别太贵，
   七十出头的」无货币单位）、1134（「四千左右的价位」模糊且无货币单位）。
   重跑时预算门槛判不可核验，属诚实降级；不猜值。
3. **未声明预算（4 个任务）**：47、232、843、1084，预算门槛按未声明通过
   （旧行为保留）。
4. **冻结 goal 预算口径差异（65 处）**：冻结 `price_upper` 为 None 或与
   budget-compile-v2 重编译结果不同（块/块钱/售价/别超/上下/多/中文数字等
   历史漏解析表达）。仅在按新协议重新生成 goal 的重跑中生效；冻结文件不改。

## 5. 完成 / 未完成 / 缺数据

**已完成（本次）**：统一终止协议（环境锁定 + eval/export 双口径）、预算
编译与门槛语义（区间下界、未声明/提取失败分离、中文数字可靠子集）、SKU
前置校验（漏选/非法/冲突区分、可信完整表契约、未知价不回填）、不可变终局
快照与出站字段保真、动态需求纯逻辑基座（幂等/冲突/撤销重算/隔离）、预检
脚本、结果覆盖保护与报告协议校验、跨层集成测试（真实 receive/step 路线）。

**后续阶段已完成**（见 `docs/RUBRIC_V2.md`）：Shopper 结构化事件与
可信同步路由（`scripts/shopper_simulator.py` / `src/shop-tools.js` /
`shop_agent.py`）、环境候选集合按需求版本重算与双口径终局、事件稳定引用
（step 重复用出现顺序映射）、rubric v2 时间线、Judge v2（四态+七维，
候选证据与最终满足分离，证据预算）、report v2（来源/分母/版本可解释）。
仍待办：循环阈值是否过严的单独审查；真实 LLM 重评与在线重跑（需授权）。

**缺数据**：第 4 节第 1 条所列 9 个商品的完整组合价。

## 6. 重跑命令（同协议重跑与离线重评）

> 以下命令均已按 `--help` 核对参数；`cd` 一律使用子 shell `( cd ... )`
> 避免污染后续相对路径；在线批次未启动。

```bash
# 0) 预检（只读，建议每次重跑前执行）
./environments/ShopSimulator/.venv-shopsim/bin/python scripts/precheck_benchmark.py \
  --benchmark benchmarks/shopping-final-v1

# 1) 离线重评历史 run（不重跑模型；新口径写独立目录，默认 v2；
#    目标目录已有结果时默认拒绝覆盖，可加 --force-overwrite）
python3 eval/evaluate.py \
  --traces runs/h0-0905-1446/traces \
  --run-dir runs/h0-0905-1446 \
  --terminal-protocol terminal-protocol-v2 \
  --out reports/h0-0905-1446-terminal-v2
# 复现历史口径：--terminal-protocol terminal-protocol-v1 --out reports/<另一新目录>

# 2) 环境单元测试（子 shell 内 cd，88 项，含跨层集成）
( cd environments/ShopSimulator/shop_env && \
  ../.venv-shopsim/bin/python -m unittest discover -s tests )

# 3) 终局协议 / 导出协议离线测试、JS 购买守卫测试
python3 eval/test_terminal_protocol.py
python3 eval/test_export_trace_protocol.py
node scripts/test_buy_guard.mjs

# 4) 完整在线重跑（后续实验，需显式授权；先起环境与 Shopper 服务，
#    再用真实 benchmark CLI。run_batch.sh 的参数是任务数量，不是 profile）
#      bash scripts/start_environment.sh
#      bash scripts/start_shopper.sh
#      python3 scripts/run_benchmark.py --help   # 核对参数
#      python3 scripts/run_benchmark.py \
#        --benchmark benchmarks/shopping-final-v1 --profile h0
#      python3 scripts/run_benchmark.py \
#        --benchmark benchmarks/shopping-final-v1 --profile h1
#    低层批量工具（可选）：bash scripts/run_batch.sh <goal_count> [goal_start]
#    重跑在新协议下自动生成新 run 目录；历史 runs/ 不受影响。

# 5) 新 run 的确定性评测、Rubric Judge 与统一报告（沿用既有管线；
#    导出 trace 默认按 terminal-protocol-v2 构造终局摘要）
python3 eval/evaluate.py --traces runs/<new-run>/traces --run-dir runs/<new-run>
python3 eval/judge.py \
  --rubrics benchmarks/shopping-final-v1/rubrics \
  --traces runs/<new-run>/traces \
  --out evaluations/shopping-final-v1/<new-run>/judgments --concurrency 4
python3 eval/report.py \
  --benchmark benchmarks/shopping-final-v1 \
  --run-dir runs/<new-run> \
  --deterministic reports/<new-run> \
  --judgments evaluations/shopping-final-v1/<new-run>/judgments \
  --out evaluations/shopping-final-v1/<new-run>/report.json
```
