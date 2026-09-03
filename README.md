# Shopping Long-Horizon Harness

面向长程购物 Agent 的可审计 Harness：在 DeepSeek Harness（dsh）之上，
把 ShopSimulator 的三个原生动作（`search` / `click` / `finish`）暴露为模型工具，
并加入 Multi-Turn + Personalization 场景的 `ask_shopper` 交互闭环与 Shopper Simulator。

## 目录结构

```
.
├── package.json                 # dsh bundle 声明（@shopping-longhorizon/shop-tools）
├── cordis.patch.yml             # bundle patch：把 shop-tools 插入 profile
├── src/shop-tools.js            # ShopSimulator 工具插件（模型可见面 / verifier 证据分离）
├── harness/h0/                  # h0 基线 profile（禁用非购物工具 + 最小购物 persona）
├── environments/ShopSimulator   # 内嵌 ShopSimulator v2 快照（自包含）
├── scripts/
│   ├── setup.sh                 # 一键安装（clone dsh + 装环境）
│   ├── setup_harness.sh         # 安装 h0 profile
│   ├── start_environment.sh     # 启动 ShopSimulator 服务 :5700
│   ├── start_shopper.sh         # 启动 Shopper Simulator 服务 :5701
│   ├── run_batch.sh             # 并行跑一批独立购物任务
│   ├── export_trace.py          # session.jsonl.zstd → 双视角 trace
│   └── shopper_simulator.py     # 模拟用户（Multi-Turn + Personalization）
└── .env.example                 # 配置模板（复制为 .env 后填写密钥）
```

## 快速开始

```bash
# 1. 一键安装（clone dsh + 装 ShopSimulator 环境）
bash scripts/setup.sh

# 2. 配置模型密钥
cp .env.example .env   # 编辑 .env，填 DEEPSEEK_API_KEY

# 3. 启动环境（另开终端，保持运行）
bash scripts/start_environment.sh

# 4. 启动 Shopper Simulator（Multi-Turn + Personalization 场景）
bash scripts/start_shopper.sh

# 5. 跑任务
bash scripts/run_batch.sh <goal_count>
```

## 工具

| 工具 | 对应动作 |
| --- | --- |
| `search` | `search[keywords]` |
| `click` | `click[value]`（打开商品 / 选规格 / `Buy Now`） |
| `finish` | `finish[reason]`（放弃购买） |
| `ask_shopper` | 向 Shopper Simulator 提问，逐步澄清需求 |

## 内嵌环境来源

`environments/ShopSimulator` 是 ShopSimulator 的嵌入快照，上游来源与排除项记录在
[`environments/ShopSimulator/EMBEDDED_SOURCE.json`](environments/ShopSimulator/EMBEDDED_SOURCE.json)。
生成的产品 JSON、搜索索引、虚拟环境和日志均不提交。
