#!/usr/bin/env bash
# 并行跑一批独立购物任务，落盘到 runs/<时间戳>/。
#
# 用法:
#   scripts/run_batch.sh <goal_count> [goal_start]
#     并行跑 goal_idx = goal_start .. goal_start+goal_count-1（每个 goal 一个独立 dsh session）
#
# 落盘结构（与串行版一致）:
#   runs/<MMDD-HHMM>/
#     sessions/<session-uuid>/session.jsonl.zstd   ← dsh 原始 session
#     traces/<goal_idx>.model_trace.json
#     traces/<goal_idx>.raw_trace.json
#     reset/<goal_idx>.json
#     manifest.json                                 ← goal_idx -> session uuid 映射
#
# 依赖: scripts/setup.sh 已跑，ShopSimulator 服务已起（scripts/start_environment.sh）
# 并发上限: 受 ShopSimulator slot 数约束（SHOPSIM_ENV_SLOTS，默认 8），
#           goal_count 超过 slot 数时，多余任务会因租不到 slot 而失败。

set -euo pipefail
shopt -s nullglob

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GOAL_COUNT="${1:?用法: run_batch.sh <goal_count> [goal_start]}"
GOAL_START="${2:-0}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

SHOPSIM_BASE_URL="${SHOPSIM_BASE_URL:-http://127.0.0.1:5700}"
DSH_CHECKOUT="${DSH_CHECKOUT:-$ROOT/deepseek-harness}"
SHARED_HOME="$ROOT/.dsh-home"
# Multi-Turn+Personalization：persona 模式下自动指向本机 Shopper Simulator。
# 显式置 none 可强制关闭。
if [[ "${SHOPPER_BASE_URL:-}" == "none" ]]; then
  SHOPPER_BASE_URL=""
elif [[ -z "${SHOPPER_BASE_URL:-}" && "${SHOPSIM_IF_PERSONA:-0}" == "1" ]]; then
  SHOPPER_BASE_URL="http://127.0.0.1:${SHOPPER_PORT:-5701}"
fi

RUN_TS="$(date +%m%d-%H%M)"
RUN_DIR="$ROOT/runs/$RUN_TS"
mkdir -p "$RUN_DIR/sessions" "$RUN_DIR/traces" "$RUN_DIR/reset"

# 单个 goal：租 slot → 独立 home 跑 dsh → 释放 slot → 拷贝 session + 导出 trace。
run_goal() {
  local idx="$1"
  local tmp_home="$RUN_DIR/.home-$idx"
  mkdir -p "$tmp_home/sessions" || { echo "goal_idx=$idx: mkdir 失败" >&2; return 0; }
  # profile 软链共享（运行时只读）；session 目录独立，避免并行时张冠李戴
  ln -s "$SHARED_HOME/profiles" "$tmp_home/profiles" 2>/dev/null || true

  # 租 slot，取任务指令（去前缀 "Instruction: "）
  local RESET_JSON ENV_IDX TASK_TEXT
  RESET_JSON="$(curl -s -X POST "$SHOPSIM_BASE_URL/api/shop_agent" \
    -H 'Content-Type: application/json' \
    -d "{\"action\":\"reset\",\"idx\":$idx}")" || {
      echo "goal_idx=$idx: reset 失败" >&2; rm -rf "$tmp_home"; return 0; }
  ENV_IDX="$(echo "$RESET_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["env_idx"])' 2>/dev/null)" || {
      echo "goal_idx=$idx: 解析 env_idx 失败" >&2; rm -rf "$tmp_home"; return 0; }
  TASK_TEXT="$(echo "$RESET_JSON" | python3 -c '
import sys, json
r = json.load(sys.stdin)["result"]
instr = r["instruction"]
instr = instr[len("Instruction: "):] if instr.startswith("Instruction: ") else instr
# persona 场景检测：初始指令 == 模糊版 instruction_simple 时注入画像。
# 泄漏过滤：剔掉 __reasoning__ / reason_key（只属于 shopper 侧）。
persona = r.get("user_persona")
simple = r.get("instruction_simple")
if isinstance(persona, dict) and persona and simple and instr.strip() == str(simple).strip():
    persona = {k: v for k, v in persona.items() if k not in ("__reasoning__",)}
    instr += "\n\n用户画像（长期偏好，仅供参考；用户当前明确表达优先于画像）：\n" + json.dumps(persona, ensure_ascii=False)
print(instr)
' 2>/dev/null)" || true
  echo "$RESET_JSON" > "$RUN_DIR/reset/$idx.json"

  # Multi-Turn 场景：预热该任务的 shopper 会话（session=任务idx）。
  if [[ -n "${SHOPPER_BASE_URL:-}" ]]; then
    curl -s -X POST "$SHOPPER_BASE_URL/start" \
      -H 'Content-Type: application/json' \
      -d "{\"session\":\"$idx\",\"idx\":$idx}" >/dev/null 2>&1 \
      || echo "goal_idx=$idx: shopper start 失败（首次提问会懒初始化）" >&2
  fi

  # 跑一个 goal（独立 DSH_HOME）
  SHOPSIM_ENV_IDX="$ENV_IDX" \
  SHOPSIM_TASK_IDX="$idx" \
  SHOPSIM_BASE_URL="$SHOPSIM_BASE_URL" \
  SHOPPER_BASE_URL="${SHOPPER_BASE_URL:-}" \
  DSH_HOME="$tmp_home" \
    bash -c "cd '$DSH_CHECKOUT' && pnpm dsh --profile ${DSH_PROFILE:-h0} \"\$1\"" _ "$TASK_TEXT" > /dev/null 2>&1 || true

  # 释放 slot
  curl -s -X POST "$SHOPSIM_BASE_URL/api/shop_agent" \
    -H 'Content-Type: application/json' \
    -d "{\"action\":\"release_one\",\"env_idx\":$ENV_IDX}" >/dev/null 2>&1 || true

  # 找出本任务在独立 home 下产生的 session
  local NEW_SESSION SESSION_UUID SESSION_FILE
  NEW_SESSION="$(find "$tmp_home/sessions" -name 'session-*' -type d 2>/dev/null | head -1)"
  if [[ -n "$NEW_SESSION" ]]; then
    SESSION_UUID="$(basename "$NEW_SESSION")"
    cp -R "$NEW_SESSION" "$RUN_DIR/sessions/$SESSION_UUID"
    SESSION_FILE="$RUN_DIR/sessions/$SESSION_UUID/session.jsonl.zstd"
    python3 "$ROOT/scripts/export_trace.py" "$SESSION_FILE" \
      --out-dir "$RUN_DIR/traces" --id "$idx" --reset "$RUN_DIR/reset/$idx.json" >/dev/null 2>&1
    echo "{\"goal_idx\":$idx,\"session\":\"$SESSION_UUID\",\"env_idx\":$ENV_IDX}" > "$RUN_DIR/manifest-line-$idx"
    echo "==> goal_idx=$idx done (session=$SESSION_UUID)"
  else
    echo "==> goal_idx=$idx WARN: 未产生新 session" >&2
  fi

  rm -rf "$tmp_home"
  return 0
}

# 并行启动所有 goal
for idx in $(seq "$GOAL_START" $((GOAL_START + GOAL_COUNT - 1))); do
  run_goal "$idx" &
done
wait

# 汇总 manifest（无 manifest-line 文件时跳过，避免 cat 无参数挂起）
MANIFEST_LINES="$(find "$RUN_DIR" -maxdepth 1 -name 'manifest-line-*' -print 2>/dev/null)"
if [[ -n "$MANIFEST_LINES" ]]; then
  cat $MANIFEST_LINES | python3 -c "
import sys, json
rows = [json.loads(l) for l in sys.stdin if l.strip()]
json.dump({'run_ts': '$RUN_TS', 'goal_start': $GOAL_START, 'goal_count': $GOAL_COUNT, 'goals': rows}, open('$RUN_DIR/manifest.json','w'), ensure_ascii=False, indent=2)
"
fi
rm -f "$RUN_DIR"/manifest-line-*
echo "==> 完成。落盘目录: $RUN_DIR"
