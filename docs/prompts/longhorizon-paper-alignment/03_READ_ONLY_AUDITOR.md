# 阶段3：独立只读环境 Auditor

## 任务

把当前“读取 Executor 工具事件的 LLM 复核器”改成论文定义的 Auditor：它必须通过
自己的只读工具重新检查当前 ShopSimulator 环境。

## 前置

阶段1和2完成。必读论文 Method 2.4、当前 `shop_agent.py`、`pack_api.py`、
`src/shop-tools.js` 与当前 Auditor 输入。

## 环境接口

新增最小只读动作，例如：

```text
action=inspect
```

返回当前公开页面/选择状态，但必须满足：

- 不增加环境 step；
- 不改变页面、选择、progress、reward 或 done；
- 不返回 gold、reward、termination_reason、私有 goal/TaskFacts；
- 只包含 Executor 本可通过当前页面看到的公开信息；
- 输出带稳定 evidence id/provenance。

建议新增 Auditor 工具 `inspect_shop_state`。它不允许 search、click、finish、购买或调用
Shopper。

## Auditor 输入

允许：original task、Task State、Contract、Contract引用的 prior audits、Executor report。

禁止：Executor raw trajectory、Executor内部 reasoning、Executor整轮 Observation 列表、reward、
gold、私有 TaskFacts。Executor report 只用于定位待检查对象，不能作为完成证据。

## Auditor 输出

保留 complete/incomplete/blocked 与 clean/suspect/violation，并增加结构化 state updates：

```json
{
  "status": "complete | incomplete | blocked",
  "integrity": "clean | suspect | violation",
  "verified_findings": [],
  "state_updates": [],
  "remaining_gaps": [],
  "evidence": []
}
```

所有 verified finding 必须引用 Auditor 自己的 inspect evidence。

## 测试

- inspect 前后环境序列化状态完全一致。
- inspect 不增加 step/progress。
- 返回体不含隐藏字段。
- Auditor请求中不含 Executor tool events/raw trajectory。
- Executor谎报选中规格时，Auditor按独立 snapshot 判 unsupported。
- Auditor无任何写工具。
- Auditor尝试输出无效 evidence ref 时被拒绝。

## 完成条件

mock 与真实 ShopSimulator 各通过一次独立审计；检查前后状态哈希一致。不要接入购买。

