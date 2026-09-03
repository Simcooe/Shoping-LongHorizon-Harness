#!/usr/bin/env bash
# 启动 Shopper Simulator 服务（默认 :5701）。
# 依赖：环境服务已带 SHOPSIM_FACTS_DIR 落盘隐藏事实（start_environment.sh 默认开启）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

export SHOPPER_PORT="${SHOPPER_PORT:-5701}"
export SHOPSIM_FACTS_DIR="${SHOPSIM_FACTS_DIR:-$ROOT/.shopper_facts}"
exec python3 "$ROOT/scripts/shopper_simulator.py"
