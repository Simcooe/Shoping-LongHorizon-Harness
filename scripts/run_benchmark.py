#!/usr/bin/env python3
"""运行 shopping-final-v1 冻结的 200 条 Multi-Turn + Personalization 任务。

读 benchmarks/shopping-final-v1/tasks.jsonl 里的 task_id（全局 goal index，
非连续），按 SHOPSIM_ENV_SLOTS（默认 8）并发限流，每条任务：

  1. reset(idx=task_id) 租 slot，拿到 env_idx 与 persona 初始指令
  2. persona 模式拼接 query（instruction_simple + 过滤后的可见画像）
  3. 预热该任务的 Shopper Simulator 会话
  4. 独立 DSH_HOME 跑 `pnpm dsh --profile <profile> "<query>"`
  5. 释放 slot
  6. 复用 scripts/export_trace.py 导出 model_trace / raw_trace

用法（在仓库根目录）:
  python3 scripts/run_benchmark.py --benchmark benchmarks/shopping-final-v1 --profile h0
  python3 scripts/run_benchmark.py --benchmark benchmarks/shopping-final-v1 --profile h1 --slots 8
  python3 scripts/run_benchmark.py --benchmark benchmarks/shopping-final-v1 --profile h0 --only 6,10,17
  python3 scripts/run_benchmark.py --benchmark benchmarks/shopping-final-v1 --profile h0 --resume

前置:
  - 环境已起：SHOPSIM_IF_PERSONA=1 bash scripts/start_environment.sh
  - Shopper 已起：bash scripts/start_shopper.sh
  - profile 已装：bash scripts/setup_harness.sh h0 （或 h1）

注意：本脚本只跑任务、落盘 trace，不做评分；失败只记录 failed 列表供手工补跑。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.interaction_router import classify_turn  # noqa: E402
from eval.purchase_verifier import (  # noqa: E402
    VERDICT_VIOLATED,
    verify_price,
    verify_quantity,
)

# --------------------------------------------------------------------------- #
# profile -> 运行时控制能力（E0/E1 不变量）
#
# 声明来源：harness/<profile>/runtime_controls.json（进程外拦截器开关，
# 与 cordis.patch.yml 的进程内插件 buy-guard 分离）。runner 读取该文件；
# 文件缺失/不可解析时回退到下方 PROFILE_RUNTIME_CONTROLS 内置表；仍未知
# 则一律 off（宁可少做，不污染基线）。
#
# 单次执行：一个任务只跑一个 dsh 进程（模型需要用户信息时通过 ask_shopper
# 工具同步拿到回复、在同一个进程内继续，不靠 runner 重跑）。
# h0 = E0 基线：无 Purchase Verifier 行为性判定。
# h1 = E1：h0 + Purchase Verifier 行为性判定；Action Guard 由
#          harness/h1/cordis.patch.yml 的 buy-guard 插件负责。
# --------------------------------------------------------------------------- #
PROFILE_RUNTIME_CONTROLS = {
    "h0": {"purchase_verifier": False},
    "h1": {"purchase_verifier": True},
}

# 进程外拦截器的已知开关键（runtime_controls.json 里只有这些键会被读取）。
_RUNTIME_CONTROL_KEYS = ("purchase_verifier",)


def load_profile_runtime_controls(profile: str) -> dict | None:
    """读取 harness/<profile>/runtime_controls.json。

    只读取已知开关键并布尔强转；文件缺失/不可解析/非 dict → None
    （调用方回退到 PROFILE_RUNTIME_CONTROLS 或全 off）。
    """
    path = REPO_ROOT / "harness" / profile / "runtime_controls.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: bool(data[k]) for k in _RUNTIME_CONTROL_KEYS if k in data}


def resolve_runtime_controls(profile: str, mode: str) -> dict:
    """按 profile + 显式开关解析运行时控制能力。

    mode：auto=读 harness/<profile>/runtime_controls.json（缺失回退内置表，
          未知 profile 一律 off）；on=全开；off=全关。
    返回 {purchase_verifier}。
    """
    declared = load_profile_runtime_controls(profile)
    if declared is None:
        declared = PROFILE_RUNTIME_CONTROLS.get(profile, {})
    base = {k: declared.get(k, False) for k in _RUNTIME_CONTROL_KEYS}
    if mode == "on":
        return {k: True for k in _RUNTIME_CONTROL_KEYS}
    if mode == "off":
        return {k: False for k in _RUNTIME_CONTROL_KEYS}
    return base

DEFAULT_SHOPSIM_BASE_URL = "http://127.0.0.1:5700"
DEFAULT_SHOPPER_BASE_URL = "http://127.0.0.1:5701"
DEFAULT_SLOTS = 8
PERSONA_PREFIX = "用户画像（长期偏好，仅供参考；用户当前明确表达优先于画像）："


# --------------------------------------------------------------------------- #
# .env 解析（对齐 run_batch.sh 的 `source .env` 语义：.env 覆盖环境）
# --------------------------------------------------------------------------- #

def load_dotenv(path: Path) -> dict[str, str]:
    env = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 去掉成对引号（bash source 不会保留引号，这里做最小兼容）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def merge_env(dotenv: dict[str, str], **overrides: str | None) -> dict[str, str]:
    """最终子进程环境 = os.environ + .env（.env 覆盖）+ 显式 overrides。"""
    env = dict(os.environ)
    env.update({k: v for k, v in dotenv.items() if v})
    for k, v in overrides.items():
        if v is None or v == "":
            env.pop(k, None)
        else:
            env[k] = v
    return env


# --------------------------------------------------------------------------- #
# HTTP 小工具
# --------------------------------------------------------------------------- #

def http_post(url: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def reset_task(base_url: str, task_id: int, timeout: int = 120) -> dict:
    """POST /api/shop_agent reset。返回整包 JSON（{"result": {...}}）。"""
    return http_post(
        f"{base_url.rstrip('/')}/api/shop_agent",
        {"action": "reset", "idx": task_id},
        timeout=timeout,
    )


def release_slot(base_url: str, env_idx: int, timeout: int = 60) -> None:
    try:
        http_post(
            f"{base_url.rstrip('/')}/api/shop_agent",
            {"action": "release_one", "env_idx": env_idx},
            timeout=timeout,
        )
    except Exception:
        # 释放失败不阻断，slot 由环境侧最终回收
        pass


def shopper_start(shopper_url: str, task_id: int, run_id: str | None = None,
                  timeout: int = 30) -> None:
    # 会话身份 = run/task（与 shop-tools 一致，避免跨 run 串话）。
    session = f"{run_id}/{task_id}" if run_id else str(task_id)
    try:
        http_post(
            f"{shopper_url.rstrip('/')}/start",
            {"session": session, "idx": task_id, "run_id": run_id},
            timeout=timeout,
        )
    except Exception:
        # 预热失败不 fatal；shop-tools 的 ask_shopper 首次提问会懒初始化
        pass


# --------------------------------------------------------------------------- #
# persona 模式 query 拼接（复刻 run_batch.sh 的逻辑）
# --------------------------------------------------------------------------- #

def build_task_text(reset_result: dict) -> str:
    instr = str(reset_result.get("instruction") or "")
    if instr.startswith("Instruction: "):
        instr = instr[len("Instruction: "):]

    persona = reset_result.get("user_persona")
    simple = reset_result.get("instruction_simple")
    if (
        isinstance(persona, dict) and persona
        and simple
        and instr.strip() == str(simple).strip()
    ):
        # 泄漏过滤：剔掉 __reasoning__（只属于 shopper 侧）
        persona = {k: v for k, v in persona.items() if k != "__reasoning__"}
        instr += (
            "\n\n" + PERSONA_PREFIX + "\n"
            + json.dumps(persona, ensure_ascii=False)
        )
    return instr


def _raw_state(raw: dict) -> dict:
    reward_detail = raw.get("reward_detail")
    return {
        "done": raw.get("done"),
        "termination_reason": raw.get("termination_reason"),
        "reward": raw.get("reward"),
        "reward_valid": raw.get("reward_valid"),
        "reward_type": reward_detail.get("reward_type") if isinstance(reward_detail, dict) else None,
        "purchase_success": reward_detail.get("purchase_success") if isinstance(reward_detail, dict) else None,
        "purchase": raw.get("purchase"),
    }


def _compute_terminal_v2(raw_steps: list[dict]) -> dict:
    """terminal-protocol-v2：第一条 done=true 即终局。"""
    for step in raw_steps:
        raw = step.get("raw") or {}
        if raw.get("done"):
            return _raw_state(raw)
    if raw_steps:
        return _raw_state(raw_steps[-1].get("raw") or {})
    return {
        "done": False, "termination_reason": None, "reward": None,
        "reward_valid": None, "reward_type": None,
        "purchase_success": None, "purchase": {},
    }


def _find_new_session(tmp_home: Path, known: set[str]) -> Path | None:
    sessions = sorted(
        (tmp_home / "sessions").glob("**/session-*"),
        key=lambda p: p.stat().st_mtime,
    )
    for s in sessions:
        if s.name not in known:
            return s
    return None


def _export_session(session_file: Path, out_dir: Path, task_id, reset_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "scripts" / "export_trace.py"),
            str(session_file), "--out-dir", str(out_dir),
            "--id", str(task_id),
            "--reset", str(reset_path),
        ],
        capture_output=True,
        text=True,
    )


def run_purchase_verifier(task_id, task_text, purchase, price_resolution):
    """用公开 query + 购买回执做确定性数量/价格核验（修复 3）。

    只读公开信息：需求文本用公开 query（不含画像/隐藏目标）；价格用
    回执或 price_resolution。返回 {quantity, price} 两个判定（带
    requirement_id / status / reason / 证据）。
    """
    selected_options = purchase.get("options") or {}
    # 回执里没有 price_resolution 时，从 purchase 记录推导最小价格解析。
    if not isinstance(price_resolution, dict):
        if purchase.get("price") is not None:
            price_resolution = {"status": "pass", "price": purchase.get("price")}
        else:
            price_resolution = {"status": "unverifiable", "price": None}
    quantity = verify_quantity(
        required_id=f"q-{task_id}",
        requirement_version=1,
        required_text=task_text,
        selected_options=selected_options,
    )
    price = verify_price(
        required_id=f"p-{task_id}",
        requirement_version=1,
        required_text=task_text,
        price_resolution=price_resolution,
        selected_options=selected_options,
        display_base_price=purchase.get("display_base_price"),
    )
    return {"quantity": quantity, "price": price}


def _save_mea_artifacts(tmp_home: Path, run_dir: Path, task_id) -> None:
    """把每个任务临时 DSH_HOME 下的 MEA 产物复制到 runs/<run-id>/mea/<task-id>/。

    只保存实验产物；不存在的 MEA 目录直接跳过；不改 h0/h1 行为。
    """
    src = tmp_home / "mea"
    if not src.is_dir():
        return
    dst = run_dir / "mea" / str(task_id)
    try:
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 单任务执行
# --------------------------------------------------------------------------- #

def run_one_task(
    task_id: int,
    *,
    profile: str,
    controls: dict,
    base_env: dict,
    run_dir: Path,
    dsh_checkout: Path,
    shared_home: Path,
    shopper_url: str | None,
) -> dict:
    """跑一条任务，返回结果记录 dict。任何异常都不向上抛，转为 failed 记录。

    controls：{purchase_verifier}，决定是否启用 E1 行为性控制（h0 全关 → 基线）。
    """
    shopsim_base_url = base_env.get("SHOPSIM_BASE_URL") or DEFAULT_SHOPSIM_BASE_URL
    result = {
        "task_id": task_id,
        "status": "failed",
        "session": None,
        "env_idx": None,
        "error": None,
        "step_count": None,
        "reward": None,
    }
    tmp_home = run_dir / f".home-{task_id}"
    log_file = run_dir / "logs" / f"{task_id}.log"
    tmp_home.mkdir(parents=True, exist_ok=True)
    (tmp_home / "sessions").mkdir(parents=True, exist_ok=True)
    # profile 软链共享（只读）；session 目录独立
    try:
        (tmp_home / "profiles").symlink_to(
            shared_home / "profiles", target_is_directory=True
        )
    except FileExistsError:
        pass

    env_idx = None
    try:
        # 1. 租 slot
        reset_json = reset_task(
            shopsim_base_url, task_id, timeout=120
        )
        reset_result = reset_json.get("result") or {}
        env_idx = reset_result.get("env_idx")
        if env_idx is None:
            result["error"] = f"reset 无 env_idx: {reset_result.get('error', reset_json)}"
            return result
        result["env_idx"] = env_idx
        # 保存 reset 原始返回（export_trace.py 需要）
        (run_dir / "reset" / f"{task_id}.json").write_text(
            json.dumps(reset_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        task_text = build_task_text(reset_result)

        # 2. 预热 shopper 会话（run/task 会话身份）
        if shopper_url:
            shopper_start(shopper_url, task_id, run_id=run_dir.name)

        # 3. 跑 dsh（独立 DSH_HOME）；同一 env/shopper 会话多轮控制（B3）。
        env = dict(base_env)
        env.update({
            "DSH_HOME": str(tmp_home),
            "SHOPSIM_ENV_IDX": str(env_idx),
            "SHOPSIM_TASK_IDX": str(task_id),
            # run 前缀进入 shopper 会话身份与事件溯源（不改变 Agent 策略）
            "SHOPSIM_RUN_ID": run_dir.name,
            "SHOPSIM_BASE_URL": shopsim_base_url,
            "SHOPPER_BASE_URL": shopper_url or "",
        })
        known_sessions: set[str] = set()
        # 单次执行：一个任务只跑一个 dsh 进程。模型需要用户信息时应通过
        # ask_shopper 工具（同步返回、回复进同一上下文），不靠 runner 重跑。
        with log_file.open("a", encoding="utf-8") as logf:
            proc = subprocess.run(
                ["pnpm", "dsh", "--profile", profile, task_text],
                cwd=str(dsh_checkout),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            logf.write(proc.stdout.decode("utf-8", "replace"))
        result["turn_count"] = 1
        final_text = ""
        # 保存 MEA 实验产物（state/rounds/evidence/manager），不改 h0/h1 行为。
        _save_mea_artifacts(tmp_home, run_dir, task_id)
        session_file = _find_new_session(tmp_home, known_sessions)
        if session_file is not None:
            known_sessions.add(session_file.name)
            export_proc = _export_session(
                session_file / "session.jsonl.zstd", run_dir / "traces",
                task_id, run_dir / "reset" / f"{task_id}.json",
            )
            if export_proc.returncode == 0:
                model_trace = json.loads(
                    (run_dir / "traces" / f"{task_id}.model_trace.json").read_text(encoding="utf-8")
                )
                raw_trace = json.loads(
                    (run_dir / "traces" / f"{task_id}.raw_trace.json").read_text(encoding="utf-8")
                )
                # 从 stdout 提取最终 assistant 文本（headless 打印最后 assistant 文本）。
                out_text = proc.stdout.decode("utf-8", "replace").strip().splitlines()
                # headless 把 reasoning 流到 stderr、最终文本打到 stdout；取最后非空行。
                final_text = next((line for line in reversed(out_text) if line.strip()), "")
                # 诊断性分类（只记录、不重跑）：模型以何种方式结束本轮。
                decision = classify_turn(model_trace, raw_trace, final_text)
                result["terminal_decision"] = decision["decision"]

        # 释放 slot
        release_slot(shopsim_base_url, env_idx)

        # 收尾判断：读取最终 raw_trace（terminal 从 raw steps 按 v2 口径计算）。
        try:
            raw_trace = json.loads(
                (run_dir / "traces" / f"{task_id}.raw_trace.json").read_text(encoding="utf-8")
            )
            result["step_count"] = raw_trace.get("step_count")
            raw_steps = raw_trace.get("steps") or []
            term = _compute_terminal_v2(raw_steps)
            result["reward"] = term.get("reward")
            # 终局/停止一致性与公开回执（纯记录性字段，两 profile 都写，
            # 不影响行为）。
            terminal_done = term.get("done") is True
            purchase = term.get("purchase") or {}
            result["environment_done"] = terminal_done
            result["purchase_asin"] = purchase.get("asin")
            result["completion_claim_valid"] = terminal_done and bool(purchase.get("asin"))
            if not terminal_done and not purchase.get("asin"):
                stop_class = "non_terminal_agent_stop"
            elif terminal_done and not purchase.get("asin"):
                stop_class = "terminal_without_receipt"
            else:
                stop_class = "terminal_with_receipt"

            # 价格解析：取第一次 done 步的 reward_detail.evidence.price_resolution。
            price_resolution = None
            for step in raw_steps:
                raw = step.get("raw") or {}
                if raw.get("done"):
                    rd = raw.get("reward_detail") or {}
                    ev = rd.get("evidence") if isinstance(rd.get("evidence"), dict) else None
                    price_resolution = (ev or {}).get("price_resolution")
                    break

            # 数量/价格确定性核验（修复 3：purchase_verifier 接线）。
            # 记录性字段 purchase_verdict 两 profile 都写；行为性后果仅 h1。
            verdict = run_purchase_verifier(
                task_id=task_id,
                task_text=task_text,
                purchase=purchase,
                price_resolution=price_resolution,
            )
            result["purchase_verdict"] = verdict
            if (
                controls.get("purchase_verifier")
                and stop_class == "terminal_with_receipt"
                and (verdict.get("quantity") or {}).get("status") == VERDICT_VIOLATED
            ):
                # 行为性后果（仅 h1）：数量不符不得算完成；不篡改环境 reward。
                stop_class = "terminal_with_receipt_but_quantity_violated"
                result["completion_claim_valid"] = False
            result["stop_class"] = stop_class
        except Exception:
            pass

        result["status"] = "done"
        result["error"] = None
        return result
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"[:300]
        if env_idx is not None:
            release_slot(shopsim_base_url, env_idx)
        return result
    finally:
        import shutil

        shutil.rmtree(tmp_home, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 进度打印（线程安全）
# --------------------------------------------------------------------------- #

class Progress:
    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.failed = 0
        self.lock = threading.Lock()
        self.t0 = time.time()

    def report(self, rec: dict):
        with self.lock:
            self.done += 1
            if rec["status"] != "done":
                self.failed += 1
            n = self.done
            total = self.total
            elapsed = time.time() - self.t0
            tag = "done" if rec["status"] == "done" else "FAIL"
            reward = rec.get("reward")
            reward_s = f"{reward:.3f}" if isinstance(reward, (int, float)) else "-"
            line = (
                f"[{n:>4}/{total}] {tag:4s} task_id={rec['task_id']:<5} "
                f"steps={rec.get('step_count') or '-'} reward={reward_s}"
            )
            if rec["status"] != "done":
                line += f" err={rec.get('error')}"
            print(line, flush=True)
            if rec["status"] == "done":
                pass
            elif rec["status"] != "done":
                # 失败时打印 dsh log 尾部（若存在）
                pass


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def read_task_ids(benchmark_dir: Path) -> list[int]:
    tasks_path = benchmark_dir / "tasks.jsonl"
    if not tasks_path.exists():
        raise SystemExit(f"找不到 {tasks_path}")
    ids = []
    with tasks_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ids.append(int(json.loads(line)["task_id"]))
    return ids


def latest_run_dir(profile: str) -> Path | None:
    pattern = f"{profile}-*"
    dirs = sorted((REPO_ROOT / "runs").glob(pattern))
    return dirs[-1] if dirs else None


def run_benchmark(args):
    benchmark_dir = Path(args.benchmark)
    manifest_path = benchmark_dir / "manifest.json"
    if manifest_path.exists():
        bm = json.loads(manifest_path.read_text(encoding="utf-8"))
        bm_id = bm.get("benchmark_id", "?")
    else:
        bm_id = benchmark_dir.name

    all_task_ids = read_task_ids(benchmark_dir)

    # --only 子集过滤
    if args.only:
        only = {int(x.strip()) for x in args.only.split(",") if x.strip()}
        task_ids = [t for t in all_task_ids if t in only]
        if not task_ids:
            raise SystemExit(f"--only 里没有匹配的 task_id（可用范围 {all_task_ids[:3]}...）")
    else:
        task_ids = list(all_task_ids)

    # --resume：复用已有 run_dir，跳过已完成的 task
    resume = args.resume
    run_dir: Path | None = None
    if resume:
        run_dir = latest_run_dir(args.profile)
        if run_dir is None:
            print(f"[resume] 未找到历史 run_dir（runs/{args.profile}-*），改为全新运行")
            resume = False
        else:
            print(f"[resume] 复用 run_dir: {run_dir}")

    if run_dir is None:
        run_ts = time.strftime("%m%d-%H%M")
        run_dir = REPO_ROOT / "runs" / f"{args.profile}-{run_ts}"

    for sub in ("sessions", "traces", "reset", "logs"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    # resume 时跳过已有 raw_trace 的 task
    skipped = []
    if resume:
        pending = []
        for t in task_ids:
            if (run_dir / "traces" / f"{t}.raw_trace.json").exists():
                skipped.append(t)
            else:
                pending.append(t)
        task_ids = pending
        print(f"[resume] 跳过已完成的 {len(skipped)} 条，待跑 {len(task_ids)} 条")

    # 环境配置
    dotenv = load_dotenv(REPO_ROOT / ".env")
    slots = args.slots or int(dotenv.get("SHOPSIM_ENV_SLOTS") or os.environ.get("SHOPSIM_ENV_SLOTS") or DEFAULT_SLOTS)
    shopper_url = (
        args.shopper_url
        or dotenv.get("SHOPPER_BASE_URL")
        or os.environ.get("SHOPPER_BASE_URL")
        or DEFAULT_SHOPPER_BASE_URL
    )
    persona_mode = args.persona_mode
    if not persona_mode:
        shopper_url = None
    # 子进程环境：os.environ + .env（.env 覆盖），带 DEEPSEEK_API_KEY 等完整凭据
    base_env = merge_env(dotenv)
    shopsim_base_url = (
        dotenv.get("SHOPSIM_BASE_URL") or os.environ.get("SHOPSIM_BASE_URL") or DEFAULT_SHOPSIM_BASE_URL
    )
    base_env["SHOPSIM_BASE_URL"] = shopsim_base_url
    dsh_checkout = Path(
        dotenv.get("DSH_CHECKOUT") or os.environ.get("DSH_CHECKOUT") or REPO_ROOT / "deepseek-harness"
    )
    shared_home = Path(
        dotenv.get("DSH_HOME") or os.environ.get("DSH_HOME") or REPO_ROOT / ".dsh-home"
    )

    print("=" * 70)
    print(f"benchmark = {bm_id}")
    print(f"profile   = {args.profile}")
    print(f"tasks     = {len(task_ids)}" + (f"（跳过 {len(skipped)}）" if skipped else ""))
    print(f"slots     = {slots}（并发）")
    print(f"persona   = {persona_mode}")
    print(f"shopper   = {shopper_url}")
    print(f"run_dir   = {run_dir}")
    print("=" * 70)

    if not task_ids:
        print("没有需要跑的任务。")
        return 0

    progress = Progress(len(task_ids))
    records: list[dict] = []
    records_lock = threading.Lock()

    controls = resolve_runtime_controls(args.profile, args.runtime_controls)
    print(f"runtime_controls = {controls}")

    def worker(task_id: int) -> dict:
        rec = run_one_task(
            task_id,
            profile=args.profile,
            controls=controls,
            base_env=base_env,
            run_dir=run_dir,
            dsh_checkout=dsh_checkout,
            shared_home=shared_home,
            shopper_url=shopper_url if persona_mode else None,
        )
        with records_lock:
            records.append(rec)
        progress.report(rec)
        return rec

    with ThreadPoolExecutor(max_workers=slots) as pool:
        futures = [pool.submit(worker, t) for t in task_ids]
        for _ in as_completed(futures):
            pass  # 结果已由 worker 内部收集并打印进度

    # 汇总 manifest
    records.sort(key=lambda r: r["task_id"])
    failed = [r["task_id"] for r in records if r["status"] != "done"]
    run_manifest = {
        "run_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark_id": bm_id,
        "profile": args.profile,
        "runtime_controls": controls,
        "task_count": len(task_ids),
        "skipped": skipped,
        "goals": records,
        "failed": failed,
        "stats": {
            "done": sum(1 for r in records if r["status"] == "done"),
            "failed": len(failed),
        },
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("=" * 70)
    print(f"完成：成功 {run_manifest['stats']['done']} / 失败 {run_manifest['stats']['failed']} / 总计 {len(task_ids)}")
    if failed:
        print(f"failed task_ids: {failed}")
        print(f"（补跑：python3 scripts/run_benchmark.py --benchmark {benchmark_dir} "
              f"--profile {args.profile} --resume --only {','.join(map(str, failed))}）")
    print(f"结果目录: {run_dir}")
    print("=" * 70)

    return 0 if not failed else 1


def main(argv):
    parser = argparse.ArgumentParser(description="运行 shopping-final-v1 冻结 benchmark")
    parser.add_argument("--benchmark", required=True, help="benchmark 目录，如 benchmarks/shopping-final-v1")
    parser.add_argument("--profile", default=None, help="dsh profile（默认 DSH_PROFILE 环境变量或 h0）")
    parser.add_argument("--slots", type=int, default=0, help="并发数（默认 SHOPSIM_ENV_SLOTS 或 8）")
    parser.add_argument("--persona-mode", action="store_true", default=True,
                        help="persona 模式（默认开，本 benchmark 固定）")
    parser.add_argument("--no-persona", action="store_true", help="关闭 persona 模式")
    parser.add_argument("--shopper-url", default=None, help="Shopper Simulator 地址（默认 http://127.0.0.1:5701）")
    parser.add_argument("--only", default=None, help="只跑逗号分隔的 task_id 子集")
    parser.add_argument("--resume", action="store_true", help="复用最近 run_dir，跳过已有 raw_trace 的任务")
    parser.add_argument(
        "--runtime-controls",
        choices=["auto", "on", "off"],
        default="auto",
        help="运行时控制开关：auto=读 harness/<profile>/runtime_controls.json"
             "（缺失回退内置表，未知 profile 一律 off）；on=全开；off=全关。"
             "门控 Purchase Verifier 的行为性控制；"
             "Completion Gate 的记录性字段两 profile 都写，行为性判定仅随 purchase_verifier；"
             "Action Guard 由 profile 的 cordis.patch.yml 负责。",
    )
    args = parser.parse_args(argv)

    if args.no_persona:
        args.persona_mode = False

    if args.profile is None:
        dotenv = load_dotenv(REPO_ROOT / ".env")
        args.profile = (
            os.environ.get("DSH_PROFILE") or dotenv.get("DSH_PROFILE") or "h0"
        )

    return run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
