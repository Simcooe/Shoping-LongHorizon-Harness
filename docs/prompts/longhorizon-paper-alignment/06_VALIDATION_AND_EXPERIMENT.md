# 阶段6：论文语义验收与配对实验

## 任务

冻结 `mea-v4-paper` 实现，证明核心语义成立，然后再与 h0、mea-v3 做同任务配对实验。
此阶段原则上不再修改 Harness；发现问题回到对应阶段修复。

## 静态/协议验收

- 每轮 Executor 请求无前轮 assistant/tool 原始历史。
- Executor episode/session id 每轮不同，ShopSimulator env session 相同。
- Auditor evidence 全部来自独立只读 inspect。
- Auditor inspect 前后环境状态哈希一致。
- completed records 全部可追溯到 clean audit evidence。
- `Buy Now` 全部具有未过期 purchase authorization。
- Contract allowed_tools、tool budget 和 timeout 有 runtime enforcement 记录。
- Manager/Auditor/Executor失败不会进入done。
- 原始 session 与全部协议产物完整保存。

## 小批次顺序

第一组：

```text
204,263,916
```

第二组：

```text
6,47,51,98,204,263,585,916,1151
```

第三组应从 h0/mea-v3 配对结果中选取：

- h0成功但mea-v3失败；
- mea-v3成功但h0失败；
- repeat_loop；
- max_steps；
- 多次ask_shopper；
- 数量/包装问题；
- 终局与购买边界问题。

## 全量实验

只有全部小批次门槛通过后，才运行同一Final-200。必须复用：

- `benchmarks/shopping-final-v1`；
- `evaluations/h0/rubrics`；
- `deterministic_v3.py`；
- `trajectory_judge_v3.py`；
- `report_v3.py`；
- 相同Executor模型与环境版本。

另行记录 Manager/Auditor/Executor token、延迟、调用次数、失败重试和总费用。

## 报告面板

保持四面板独立：环境结果、用户需求满足、七维过程质量、确定性行为。增加成本表，但不
生成加权总分。

必须给出逐任务配对：成功gain/loss、requirements resolved gain/loss、七维改善/退化、
非法动作与终局行为变化。

## 通过门槛

不预设必须超过 h0 的统计阈值，但只有在机制验收通过后才能解释性能差异。若环境成功未
提升，应作为真实负结果保留，不能通过修改Rubric/Judge或删除失败任务修饰结论。

