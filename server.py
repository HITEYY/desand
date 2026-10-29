"""Web panel for starting, chatting with, and stopping agents. Run: python server.py  ->  http://127.0.0.1:8000"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import ask, load_agent, to_openai_messages

HERE = Path(__file__).parent
running = {}  # name -> agent
lock = threading.Lock()


def available() -> list:
    return sorted(p.stem for p in (HERE / "agents").glob("*.py") if p.stem != "__init__")


def view(a) -> dict:
    return {"name": a.name, "provider": a.provider, "model": a.model, "busy": a.busy, "error": a.error,
            "messages": to_openai_messages(list(a.messages))}  # copy: a worker thread may be appending


def send(a, text: str) -> None:
    a.busy = True
    try:
        ask(a, text)
    finally:
        a.busy = False


class Handler(BaseHTTPRequestHandler):
    def reply(self, code: int, body, ctype="application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self.reply(200, (HERE / "panel.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            with lock:
                agents = list(running.values())
            return self.reply(200, {"available": available(), "running": [view(a) for a in agents]})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        # Block other websites from driving the agents: JSON content-type forces a CORS preflight (never answered),
        # and the Host check stops DNS rebinding.
        if (not (self.headers.get("Content-Type") or "").startswith("application/json")
                or self.headers.get("Host") not in ("127.0.0.1:8000", "localhost:8000")):
            return self.reply(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or "{}")
        except json.JSONDecodeError:
            return self.reply(400, {"error": "bad json"})
        name = body.get("name")
        with lock:
            if self.path == "/api/start":
                if name not in available():
                    return self.reply(400, {"error": f"unknown agent: {name}"})
                if name in running:
                    return self.reply(400, {"error": f"{name} already running"})
                try:
                    running[name] = load_agent(name)
                except BaseException as e:  # load_agent uses SystemExit for missing keys/models
                    return self.reply(400, {"error": str(e)})
                running[name].busy = False
            elif self.path == "/api/stop":
                running.pop(name, None)  # an in-flight turn finishes in the background
            elif self.path == "/api/reset":
                if name in running and not running[name].busy:
                    running[name].messages.clear()
            elif self.path == "/api/send":
                text = (body.get("text") or "").strip()
                targets = [running[n] for n in body.get("names", []) if n in running and not running[n].busy]
                if not text or not targets:
                    return self.reply(400, {"error": "need text and at least one idle running agent"})
                for a in targets:
                    threading.Thread(target=send, args=(a, text), daemon=True).start()
            else:
                return self.reply(404, {"error": "not found"})
        self.reply(200, {"ok": True})

    def log_message(self, *args):  # keep the terminal for agent output
        pass


if __name__ == "__main__":
    # Local only: agents can read/write files in this directory and there is no auth.
    # HOST=0.0.0.0 only inside a container, published to the host's loopback (see README).
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Agent panel: http://127.0.0.1:8000 (listening on {host})")
    ThreadingHTTPServer((host, 8000), Handler).serve_forever()
