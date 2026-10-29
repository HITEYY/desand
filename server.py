"""Web panel for starting, chatting with, and stopping agents. Run: python server.py  ->  http://127.0.0.1:8000"""
import os
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core import PROVIDERS, WORKSPACES, agent_defaults, ask, list_models, load_agent, to_openai_messages

HERE = Path(__file__).parent
running = {}  # name -> agent
lock = threading.Lock()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
# Host check stops DNS rebinding: other sites can't reach the panel through a hostname they control.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


class Name(BaseModel):
    name: str


class Start(BaseModel):
    name: str
    workspace: str
    model: str = ""


class Key(BaseModel):
    env: str
    value: str


class Message(BaseModel):
    names: list[str]
    text: str


def json_only(request: Request) -> None:
    # A JSON content-type forces a CORS preflight (never answered), so other sites can't POST here.
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(403, "forbidden")


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, exc.status_code)


def available() -> list:
    return sorted(p.stem for p in (HERE / "agents").glob("*.py") if p.stem != "__init__")


def agent_info(name: str) -> dict:
    _, provider, model = agent_defaults(name)
    return {"name": name, "provider": provider, "model": model}


def workspaces() -> list:
    return sorted(p.name for p in WORKSPACES.glob("*") if p.is_dir()) if WORKSPACES.is_dir() else []


def key_names() -> list:
    return sorted({c["key_env"] for c in PROVIDERS.values()})


def view(a) -> dict:
    return {"name": a.name, "provider": a.provider, "model": a.model, "workspace": a.workspace, "busy": a.busy, "error": a.error,
            "messages": to_openai_messages(list(a.messages))}  # copy: a worker thread may be appending


def send(a, text: str) -> None:
    a.busy = True
    try:
        ask(a, text)
    finally:
        a.busy = False


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"available": available()})


@app.get("/api/state")
def state() -> dict:
    with lock:
        agents = list(running.values())
    return {"available": [agent_info(n) for n in available()], "workspaces": workspaces(),
            "running": [view(a) for a in agents],
            "keys": {k: bool(os.environ.get(k)) for k in key_names()}}  # set/unset only, never the value


@app.get("/api/models")
def models(provider: str) -> list:
    if provider not in PROVIDERS:
        raise HTTPException(400, f"unknown provider: {provider}")
    return list_models(provider)


@app.post("/api/start", dependencies=[Depends(json_only)])
def start(body: Start) -> dict:
    with lock:
        if body.name not in available():
            raise HTTPException(400, f"unknown agent: {body.name}")
        if body.name in running:
            raise HTTPException(400, f"{body.name} already running")
        try:
            agent = load_agent(body.name, model=body.model.strip() or None, workspace=body.workspace.strip())
        except BaseException as e:  # load_agent uses SystemExit for missing keys/models
            raise HTTPException(400, str(e))
        agent.busy = False
        running[body.name] = agent
    return {"ok": True}


# ponytail: keys live in this process's env only (lost on restart); not written to disk because
# agents' file tools can read the working directory. Running agents keep their old key until restarted.
@app.post("/api/key", dependencies=[Depends(json_only)])
def set_key(body: Key) -> dict:
    if body.env not in key_names():
        raise HTTPException(400, f"unknown key: {body.env}")
    value = body.value.strip()
    if value:
        os.environ[body.env] = value
    else:
        os.environ.pop(body.env, None)
    return {"ok": True}


@app.post("/api/stop", dependencies=[Depends(json_only)])
def stop(body: Name) -> dict:
    with lock:
        running.pop(body.name, None)  # an in-flight turn finishes in the background
    return {"ok": True}


@app.post("/api/reset", dependencies=[Depends(json_only)])
def reset(body: Name) -> dict:
    with lock:
        if body.name in running and not running[body.name].busy:
            running[body.name].messages.clear()
    return {"ok": True}


@app.post("/api/send", dependencies=[Depends(json_only)])
def send_message(body: Message) -> dict:
    text = body.text.strip()
    with lock:
        targets = [running[n] for n in body.names if n in running and not running[n].busy]
        if not text or not targets:
            raise HTTPException(400, "need text and at least one idle running agent")
        for a in targets:
            a.busy = True  # mark now so a quick second send can't double-start the agent
            threading.Thread(target=send, args=(a, text), daemon=True).start()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    # Local only: agents can read/write files in the working directory and there is no auth.
    # HOST=0.0.0.0 only inside a container, published to the host's loopback (see README).
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Agent panel: http://127.0.0.1:8000 (listening on {host})")
    uvicorn.run(app, host=host, port=8000, log_level="warning")
