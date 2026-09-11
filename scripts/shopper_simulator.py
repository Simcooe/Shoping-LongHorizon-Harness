#!/usr/bin/env python3
"""Shopper Simulator —— Multi-Turn+Personalization 场景的模拟用户服务。

读取环境侧落盘的隐藏事实（$SHOPSIM_FACTS_DIR/<idx>.json），用一个 LLM
扮演用户：只答所问、逐步透露、可拒绝/修改需求、购买确认绑定完整需求。

纯 stdlib + DeepSeek Chat API（key 来自项目根 .env），用法：
  python3 scripts/shopper_simulator.py            # 默认 :5701
接口：
  POST /start {session, idx}   初始化会话（读 idx 的隐藏事实）
  POST /ask   {session, question} -> {reply}
  POST /end   {session}
  GET  /health
"""
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = REPO_ROOT / ".env"

DEFAULT_MODEL = os.environ.get("SHOPPER_MODEL", "deepseek-v4-pro")
DEFAULT_BASE_URL = "https://api.deepseek.com"
FACTS_DIR = Path(os.environ.get("SHOPSIM_FACTS_DIR", REPO_ROOT / ".shopper_facts"))
PORT = int(os.environ.get("SHOPPER_PORT", "5701"))
MAX_TURNS = 40

# 会话表：session -> {"facts": ..., "history": [...]}
SESSIONS = {}
SESSIONS_LOCK = threading.RLock()


def load_dotenv(path=DEFAULT_ENV):
    env = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


_ENV = load_dotenv()
API_KEY = os.environ.get("SHOPPER_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") \
    or _ENV.get("SHOPPER_API_KEY") or _ENV.get("DEEPSEEK_API_KEY") or ""
BASE_URL = os.environ.get("SHOPPER_BASE_URL_LLM") or os.environ.get("DEEPSEEK_BASE_URL") \
    or _ENV.get("SHOPPER_BASE_URL_LLM") or _ENV.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL

SYSTEM_TEMPLATE = """你是一位真实的电商购物用户，正在和购物助手对话。你要自然地扮演这个用户。

【你内心真正想要的（只有你知道，不要主动全盘托出）】
完整需求：{instruction_full}
关键属性：{attributes}
目标规格：{goal_options}
预算上限：{price_upper}
你的长期画像：{persona}
{reasoning}

【行为规则】
1. 只回答对方问到的内容；没被问到的需求细节不要主动透露。
2. 每次回答最多补充 1~2 个细节，像真实用户一样逐步说清需求。
3. 回答必须与上面"内心需求"一致；不编造新需求，不前后矛盾；已透露过的信息保持一致。
4. 助手推荐的商品/规格若明显不满足你已透露的需求，可以拒绝并给简短理由。
5. 你可以补充或修改需求（如"预算改成800"），之后以新说法为准。
6. 助手向你确认是否购买某商品某规格时：只有满足你的完整需求（含目标规格和预算）才确认，否则指出不满意的地方。
7. 你不知道也不许提及任何商品编号(ASIN)、店铺名或"标准答案"；你只知道自己的需求。
8. 回复口语化、简短（不超过80字）；不暴露自己是 AI 或有脚本。"""


def build_system_prompt(facts):
    persona = {k: v for k, v in (facts.get("persona") or {}).items()}
    reasoning = facts.get("persona_reasoning") or ""
    if reasoning:
        reasoning = f"画像背后的原因（帮助你保持人设一致）：{reasoning}"
    price = facts.get("price_upper")
    price_text = f"{price} 元" if price not in (None, "") else "未明确（被问到时按完整需求回答）"
    return SYSTEM_TEMPLATE.format(
        instruction_full=facts.get("instruction_full") or "",
        attributes="、".join(facts.get("attributes") or []) or "无",
        goal_options="；".join(facts.get("goal_options") or []) or "无",
        price_upper=price_text,
        persona=json.dumps(persona, ensure_ascii=False) if persona else "无",
        reasoning=reasoning,
    )


def chat(messages):
    url = BASE_URL.rstrip("/") + "/chat/completions"
    payload = {"model": DEFAULT_MODEL, "messages": messages, "temperature": 0.3}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        obj = json.loads(resp.read().decode("utf-8"))
    return obj["choices"][0]["message"]["content"].strip()


def do_ask(session, question):
    with SESSIONS_LOCK:
        s = SESSIONS.get(session)
        if s is None:
            return None
        s["history"].append({"role": "user", "content": question})
        if len(s["history"]) > MAX_TURNS * 2:
            s["history"] = s["history"][-MAX_TURNS * 2:]
        messages = [{"role": "system", "content": s["system"]}] + list(s["history"])
    reply = chat(messages)
    with SESSIONS_LOCK:
        current = SESSIONS.get(session)
        if current is None or current is not s:
            return None
        current["history"].append({"role": "assistant", "content": reply})
    return reply


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "sessions": len(SESSIONS)})
        self._send(404, {"error": "not_found"})

    def do_POST(self):
        data = self._body()
        session = str(data.get("session", ""))
        if self.path == "/start":
            idx = data.get("idx")
            if not session:
                return self._send(400, {"error": "empty_session"})
            facts_path = FACTS_DIR / f"{idx}.json"
            if not facts_path.is_file():
                return self._send(404, {"error": "no_facts", "detail": str(facts_path)})
            with SESSIONS_LOCK:
                existing = SESSIONS.get(session)
                if existing is not None:
                    if str(existing.get("idx")) != str(idx):
                        return self._send(409, {"error": "session_identity_conflict"})
                    return self._send(200, {
                        "ok": True, "session": session, "existing": True,
                        "turns": len(existing["history"]) // 2,
                    })
                facts = json.loads(facts_path.read_text(encoding="utf-8"))
                SESSIONS[session] = {
                    "idx": idx,
                    "facts": facts,
                    "system": build_system_prompt(facts),
                    "history": [],
                }
            return self._send(200, {"ok": True, "session": session, "existing": False})
        if self.path == "/ask":
            with SESSIONS_LOCK:
                known = session in SESSIONS
            if not known:
                return self._send(404, {"error": "unknown_session"})
            question = str(data.get("question", "")).strip()
            if not question:
                return self._send(400, {"error": "empty_question"})
            try:
                reply = do_ask(session, question)
            except Exception as e:
                return self._send(502, {"error": "llm_failed", "detail": str(e)[:300]})
            return self._send(200, {"reply": reply})
        if self.path == "/end":
            with SESSIONS_LOCK:
                SESSIONS.pop(session, None)
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[shopper] " + (fmt % args) + "\n")


def main():
    if not API_KEY:
        print("缺少 SHOPPER_API_KEY / DEEPSEEK_API_KEY（检查 .env）", file=sys.stderr)
        sys.exit(1)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[shopper] facts dir: {FACTS_DIR}", file=sys.stderr)
    print(f"[shopper] model: {DEFAULT_MODEL} @ {BASE_URL}", file=sys.stderr)
    print(f"[shopper] listening on 127.0.0.1:{PORT}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
