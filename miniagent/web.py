"""Web 表面层（教程 Ch.8：CLI / ACP 之外的第三种表面）。

GET  /           查询表单页
POST /api/query  {"text": "..."} → 在线运行 agent，返回结果/工具轨迹/用量账本

安全边界：并发闸（默认 1，防 API 限速雪崩）+ 输入长度护栏（loop 层已有 20 万字符）
+ 每次请求独立 workdir + 后台 key 只走环境变量（绝不进页面/响应）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .loop import Agent

MAX_CONCURRENT = int(os.environ.get("MINIAGENT_WEB_CONCURRENCY", "1"))
TASK_TIMEOUT = int(os.environ.get("MINIAGENT_WEB_TIMEOUT", "600"))
WEB_ROOT = os.environ.get("MINIAGENT_WEB_ROOT",
                          os.path.join(tempfile.gettempdir(), "miniagent_web"))

_sem = threading.Semaphore(MAX_CONCURRENT)

FORM = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>miniagent · 在线运行</title>
<style>
body{margin:0;background:#0f1419;color:#d7e0e8;font:15px/1.7 "Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:36px 20px 60px}
h1{color:#fff;font-size:24px}.sub{color:#8b9aab;margin-bottom:18px}
a{color:#4fb3ff}
textarea{width:100%;min-height:120px;background:#0a0e12;color:#d7e0e8;border:1px solid #2b3744;border-radius:8px;padding:12px;font:14px/1.6 Consolas,monospace;box-sizing:border-box}
button{margin-top:12px;background:#4fb3ff;color:#06121c;border:none;border-radius:8px;padding:10px 26px;font-size:15px;font-weight:700;cursor:pointer}
button:disabled{background:#2b3744;color:#8b9aab}
.card{background:#1a222b;border:1px solid #2b3744;border-radius:10px;padding:16px 20px;margin:16px 0;white-space:pre-wrap;word-break:break-word}
.meta{color:#3fd68f;font-size:13px}
.err{color:#ff6b6b}
.tools span{display:inline-block;background:#22303d;border:1px solid #2b3744;border-radius:20px;padding:1px 10px;font-size:12px;color:#4fb3ff;margin:2px 4px 2px 0}
</style></head><body><div class="wrap">
<h1>miniagent · 在线运行</h1>
<div class="sub">最小完备 coding agent · 后端 qwen3.7-max · <a href="/miniagent/">返回入口</a> · 每次请求独立工作目录</div>
<textarea id="t" placeholder="例如:创建一个 fib.py 实现斐波那契并写 unittest 跑通"></textarea><br>
<button id="go" onclick="run()">运行</button>
<div id="out"></div>
<script>
async function run(){
  const t = document.getElementById('t').value.trim();
  if(!t) return;
  const btn = document.getElementById('go'), out = document.getElementById('out');
  btn.disabled = true; btn.textContent = '运行中(最长 10 分钟)...';
  out.innerHTML = '';
  try{
    const r = await fetch('/api/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
    const d = await r.json();
    if(d.ok){
      const tools = (d.tools||[]).map(x=>`<span>${x}</span>`).join('');
      out.innerHTML = `<div class="card meta">用量: prompt ${d.usage.prompt_tokens} + completion ${d.usage.completion_tokens} tokens · LLM 调用 ${d.usage.calls} 次 · 工具调用 ${d.steps} 次</div>`
        + (tools?`<div class="card tools">${tools}</div>`:'')
        + `<div class="card">${d.result.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</div>`;
    }else{
      out.innerHTML = `<div class="card err">${d.error||'运行失败'}</div>`;
    }
  }catch(e){ out.innerHTML = `<div class="card err">网络错误: ${e}</div>`; }
  btn.disabled = false; btn.textContent = '运行';
}
</script>
</div></body></html>"""


def run_task(text: str) -> dict:
    """在独立 workdir 里跑一次 agent，收集结果/工具轨迹/用量。"""
    wd = os.path.join(WEB_ROOT, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                      + "-" + uuid.uuid4().hex[:6])
    os.makedirs(wd, exist_ok=True)
    agent = Agent(wd, permission_mode="auto")
    result = agent.run(text)
    tools: list[str] = []
    for e in agent.transcript.resume():
        if e.get("type") != "message":
            continue
        m = e["message"]
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                tools.append((tc.get("function") or {}).get("name", "?"))
    return {"result": result, "usage": agent.usage,
            "tools": tools, "steps": len(tools)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: str,
              ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, FORM)
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if self.path != "/api/query":
            return self._send(404, "not found", "text/plain; charset=utf-8")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 1_000_000:
            return self._json(400, {"ok": False, "error": "请求体长度非法"})
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            text = str(payload.get("text") or "").strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
        if not text:
            return self._json(400, {"ok": False, "error": "任务为空"})

        if not _sem.acquire(blocking=False):
            return self._json(429, {"ok": False,
                                    "error": "当前有任务在运行，请稍后重试（并发闸=1）"})
        box: dict = {}

        def work() -> None:
            try:
                box["r"] = run_task(text)
            except Exception as e:  # agent 内部异常不拖死服务
                box["e"] = f"{type(e).__name__}: {e}"
            finally:
                # Python 线程无法安全强杀；超时响应后仍保持占位，直到后台任务实际结束。
                _sem.release()

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(TASK_TIMEOUT)
        if t.is_alive():
            return self._json(504, {"ok": False,
                                    "error": f"任务超时（{TASK_TIMEOUT}s），已放弃等待"})
        if "e" in box:
            return self._json(500, {"ok": False, "error": box["e"][:500]})
        self._json(200, {"ok": True, **box["r"]})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[web] {fmt % args}", file=sys.stderr)


def main() -> None:
    port = int(os.environ.get("MINIAGENT_WEB_PORT", "19120"))
    os.makedirs(WEB_ROOT, exist_ok=True)
    print(f"[web] miniagent web listening on :{port}, workroot={WEB_ROOT}",
          file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
