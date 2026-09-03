#!/usr/bin/env bash
# 安装 h0 原生购物 harness 为 dsh 的一个 profile。
#
# 用法:
#   bash scripts/setup_harness.sh            # 安装到 $DSH_HOME/profiles/h0
#   bash scripts/setup_harness.sh <name>     # 安装到指定 profile 名
#
# 前置: scripts/setup.sh 已跑（dsh 已装、ShopSimulator 环境已装）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSH_HOME="${DSH_HOME:-$ROOT/.dsh-home}"
DSH_CHECKOUT="${DSH_CHECKOUT:-$ROOT/deepseek-harness}"
PROFILE_NAME="${1:-h0}"

export DSH_HOME

# 1. 用 dsh plugin add 初始化 profile，并 link 可编辑面 bundle（@self-harness-dsh/shop-tools）
( cd "$DSH_CHECKOUT" && pnpm dsh plugin --profile "$PROFILE_NAME" add "$ROOT" )

# 2. 覆盖 cordis.patch.yml 为 h0 的最小 spine
PROFILE_DIR="$DSH_HOME/profiles/$PROFILE_NAME"
cp "$ROOT/harness/h0/cordis.patch.yml" "$PROFILE_DIR/cordis.patch.yml"

# 3. 覆盖 package.json，确保 bundles 只含 shop-tools（去掉 plugin add 可能写入的其他字段差异）
cp "$ROOT/harness/h0/package.json" "$PROFILE_DIR/package.json"

echo "==> h0 已安装为 profile '$PROFILE_NAME'"
echo "    验证: DSH_HOME=$DSH_HOME pnpm dsh --profile $PROFILE_NAME --dump-config"
echo "    运行: bash scripts/run_task.sh \"<任务>\"  (但 run_task.sh 仍用 headless，需按需调整)"
