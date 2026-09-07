# Rubric v2 / Judge v2 / Report v2 实施说明

依据 `docs/prompts/rubric_v2_implementation.md` 实施。基础补漏
（`docs/prompts/repair_followup.md`，见 `docs/PROTOCOL_V2.md`）已完成并经
94 项环境测试与 5 组离线测试验证；本阶段贯通：

```
初始需求 → Shopper 可见回复+结构化事件 → 可信同步 → 环境双口径判定
        → 带事件版本的 trace → rubric v2 时间线 → Judge v2 → report v2
历史任务：原始 trace 保留，只重建事件（标注 retrospective）→ 同一编译链
```

## 1. 三种需求视角（实现落点）

| 视角 | 实现 |
|---|---|
| 初始公开约束 | `benchmarks/.../rubrics/<tid>.json`（v1，不动）→ `base_constraints_to_v2()` 转入 v2，保留原 ID/来源映射 |
| Agent 已知有效约束 | `TimelineCompiler.effective_set()`（初始 + 截至决策时刻的已收事件）；过程/最终评分只用它，不回溯 |
| 环境真实有效目标 | 环境 `evaluate_purchase(goal)` 初始静态口径保留为 `initial_static_result`；可信修改经账本后计算 `active_requirement_result`；隐藏需求不因未被问到而消失 |

事件类型：`reveal / add / modify / conditional_accept / reject / revoke`
（`eval/requirement_schema.py`，与环境 `engine/requirements.py` 共享枚举）。

## 2. Schema 与字段契约

### 事件（`shopping-requirement-events-v1`）
`event_id`（稳定，不以 step 编号充当）、`session`（run/task）、`kind`、
`requirement_key`、`old_value/new_value`、`source_reply_id/source_quote`、
`conditions`、`scope`、`references_event`、`expected_version/result_version`、
`seq`、`status`（applied/duplicate/conflict/version_mismatch/
pending_confirmation/post_terminal_excluded/evidence_unmapped/rejected_invalid）、
`step_index`（trace 出现顺序位置；无法唯一映射时保持 null 并显式报缺证据）。

### 动态 rubric（`shopping-rubric-schema-v2`）
`constraints[]`：`id / requirement_key / description / hardness / source /
source_event_id / source_quote / visible_from_event / effective_from_event /
effective_until_event / supersedes / lifecycle_status(active|superseded|revoked)
/ scope / conditions / value / source_events`；
`conditional_permissions[]`（单笔报价许可，带 asin/规格/金额 scope）；
`requirement_versions[]`；`decision{kind, position, requirement_version}`；
`final_constraint_ids / final_permission_ids`；`skipped_events`；
`base_rubric_hash / trace_hash / events_hash / input_fingerprint`。

### Judge v2 输出（`shopping-judge-output-v2`）
`candidate_evidence[]`（候选页证据，≠最终满足）；
`final_requirement_verdicts[]`（四态 + `requirement_version` +
`position_reference` + `event_reference` + reasoning）；
`dimension_scores`（七维 0/1/2）+ `dimension_reasons`；
metadata：`judge_mode(llm|mock) / model / evidence_coverage(含
evidence_truncated) / decision / requirement_version_at_decision`。

## 3. 在线接线（本次已实现，真实批次待授权）

- `scripts/shopper_simulator.py`：每次回复同时产出结构化事件
  （与离线重建共用 `HeuristicExtractor`，同一回复不被两个提取器各解释一次）；
  事件按 session 持久化（`$SHOPSIM_EVENTS_DIR`）支持重启/重试；
  `mock_reply` 测试通道 + `SHOPPER_ALLOW_NO_KEY=1`。
- `src/shop-tools.js`：会话身份 `run/task`（不再只用 task_idx）；
  `/ask` 返回的 events 自动走环境 `sync_requirement_events`（不是模型可见
  工具），事件与同步诊断进 `presentationMeta`。
- `environments/.../web_agent_text_env.py` + `shop_agent.py` + `pack_api.py`：
  可信同步接口（会话归属/幂等/冲突/版本校验；终局锁定后拒绝）；同步后按
  生效需求**重算**候选可接受集合（预算收紧移除超预算候选、reject 按 asin
  排除，不只追加）；购买终局输出 `initial_static_result` +
  `active_requirement_result`（无同步更新/证据不足 → unavailable，不猜），
  随 `terminal_snapshot` 冻结。
- `scripts/run_benchmark.py`：子进程注入 `SHOPSIM_RUN_ID`（不改策略）。
- `scripts/export_trace.py`：从 tool-result meta 收集运行时需求事件与同步
  诊断到 `raw_trace.requirement_events`；终局摘要按声明的终止协议构造。

真实调用点：Shopper 回复生成与「语义提取升级为 LLM」需要 LLM API
（`.env` 凭据路径已复用，不输出密钥）；本次交付以确定性提取 +
`mock_reply` 证明全链路。

## 4. 离线（历史任务）链路

1. `eval/reconstruct_events.py`：从 model_trace 的 ask_shopper 问答重建事件；
   第一次真实 done 之后的问答标 `post_terminal` 不进入有效约束；
   **输出标注 `event_source=retrospective`**，不声称环境当时已采用这些需求。
2. `eval/gen_rubric_v2.py`：base + events → 时间线编译（确定性重放；
   重复澄清合并同语义要求不加分母；modify 走 supersede；单笔许可不顶替
   全局约束；final 集合取决策时刻）。缓存按输入指纹复用，`--force` 重建。
3. `eval/judge.py --rubric-version v2`：mock（确定性，链路验证，显式标注，
   不写正式 evaluations 冒充真实模型）/ llm（真实路径，需凭据）。
   证据预算替代固定截断（默认 120000 字符，超预算按步压缩并输出
   `evidence_truncated`）；ask_shopper 回复不算商品证据（避免循环自证）；
   未购买 → 最终满足状态 unknown，候选证据单独保留。
4. `eval/report.py --rubric-version v2`：输出 `report_v2.json /
   summary_v2.json / report_v2.md`，不覆盖任何 v1 文件。

## 5. 已执行的真实运行（本次交付内）

- 94 项环境测试（含在线同步集成：916 式条件许可只在作用范围放宽、
  170/其它商品/缺规格不放宽、终局锁定拒绝同步、候选集重算、双口径终局）。
- 离线测试：时间线/生命周期/冲突/幂等/撤销重放、重建（重复 step/缺 raw/
  post_terminal/unmapped）、端到端链路（916/1092/6/196/263/343 语义断言）。
- **h0-0905-1446 全 200 条离线链路**（retrospective + mock，非真实模型）：
  - 事件重建 200 任务：41 事件，5 条终局后事件排除；
  - rubric v2 200/200 成功（693 条最终约束，16 条澄清约束进时间线，
    15 条被 supersede）；
  - judge v2 mock 200/200；report v2：final_satisfaction_rate 0.694
    （分母 693，不含 N/A 与生命周期排除项），初始对照 0.694（分母 677）。

## 6. 命令

```bash
# 生成/重建动态 rubric（历史离线）
python3 eval/reconstruct_events.py \
  --benchmark benchmarks/shopping-final-v1 \
  --traces runs/<run>/traces --run-id <run> \
  --out evaluations/shopping-final-v1/<run>/events
python3 eval/gen_rubric_v2.py \
  --benchmark benchmarks/shopping-final-v1 \
  --base-rubrics benchmarks/shopping-final-v1/rubrics \
  --events evaluations/shopping-final-v1/<run>/events \
  --traces runs/<run>/traces --run-id <run> \
  --out evaluations/shopping-final-v1/<run>/rubrics_v2

# Judge v2（mock 链路验证 / llm 真实评测）
python3 eval/judge.py --rubric-version v2 \
  --rubrics-v2 evaluations/shopping-final-v1/<run>/rubrics_v2 \
  --traces runs/<run>/traces \
  --judge-mode mock --out evaluations/shopping-final-v1/<run>/judgments_v2_mock
python3 eval/judge.py --rubric-version v2 \
  --rubrics-v2 evaluations/shopping-final-v1/<run>/rubrics_v2 \
  --traces runs/<run>/traces \
  --judge-mode llm --out evaluations/shopping-final-v1/<run>/judgments_v2

# report v2
python3 eval/report.py --rubric-version v2 \
  --benchmark benchmarks/shopping-final-v1 \
  --deterministic reports/<run>-terminal-v2 \
  --rubrics-v2 evaluations/shopping-final-v1/<run>/rubrics_v2 \
  --judgments-v2 evaluations/shopping-final-v1/<run>/judgments_v2 \
  --events evaluations/shopping-final-v1/<run>/events \
  --out evaluations/shopping-final-v1/<run>

# 在线新 run（同协议 h0/h1；需先起环境与 Shopper，真实批次需授权）
#   bash scripts/start_environment.sh && bash scripts/start_shopper.sh
#   python3 scripts/run_benchmark.py --benchmark benchmarks/shopping-final-v1 --profile h0
#   python3 scripts/run_benchmark.py --benchmark benchmarks/shopping-final-v1 --profile h1
```

## 7. 限制与尚未执行

- **未执行真实 LLM 批次**：200 条 judgments_v2_mock 为确定性链路验证，
  不是真实模型成绩；真实重评按第 6 节 `--judge-mode llm` 执行。
- **未重跑在线 Agent**：环境已具备双口径与事件链路，h0/h1 同协议重跑
  需显式授权后执行；历史结果均为离线重解释（retrospective）。
- 缺 SKU 完整组合价的 9 个商品（见 `docs/PROTOCOL_V2.md` §4）不阻塞本
  链路：价格不可核验 → unknown/unavailable，不猜价。
- 启发式语义提取只覆盖高置信模式（预算数字、拒绝）；「必须调光」类
  硬需求、条件接受的 asin 绑定等需要 LLM 提取器（接口已留：
  `--events-from` 外部事件、`HeuristicExtractor` 可替换）。
- v1 Judge/report 口径保留可用；v1 与 v2 输出目录/文件名隔离，不混用。
