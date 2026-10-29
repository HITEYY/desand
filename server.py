"""Web panel for starting, chatting with, and stopping agents. Run: python server.py  ->  http://127.0.0.1:8000"""
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core import (PROVIDERS, S3_ENV, TOOL_BY_NAME, WORKSPACES, agent_defaults, ask, file_agents, list_models,
                  load_agent, load_config, s3_target, to_openai_messages, update_config, upload_trajectories,
                  valid_name, workspace_dir)

HERE = Path(__file__).parent
running = {}  # name -> agent
lock = threading.Lock()
PASSWORD = os.environ.get("PANEL_PASSWORD") or secrets.token_urlsafe(16)  # random per run if unset; printed at startup
SESSION = secrets.token_urlsafe(32)  # login cookie value; a restart logs everyone out


def auth(panel_session: str = Cookie("")) -> None:
    # The cookie is bound to this host, so a DNS-rebinding hostname never carries it.
    if not secrets.compare_digest(panel_session.encode(), SESSION.encode()):
        raise HTTPException(401, "login required")


def json_only(request: Request) -> None:
    # A JSON content-type forces a CORS preflight (never answered), so other sites can't POST here.
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(403, "forbidden")


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")  # static files stay public
templates = Jinja2Templates(directory=HERE / "templates")
api = APIRouter(dependencies=[Depends(auth)])


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 401 and not request.url.path.startswith("/api/"):
        return RedirectResponse("/login", 303)
    return JSONResponse({"error": exc.detail}, exc.status_code, headers=exc.headers)


class Name(BaseModel):
    name: str


class Login(BaseModel):
    password: str


class Start(BaseModel):
    name: str
    workspace: str
    model: str = ""  # saved model name; empty = the agent's own default


class Key(BaseModel):
    env: str
    value: str


class Model(BaseModel):
    name: str
    provider: str
    model: str


class Agent(BaseModel):
    name: str
    model: str
    system: str = ""
    tools: list[str] = []


class Message(BaseModel):
    names: list[str]
    text: str


def available() -> list:
    return sorted({*file_agents(), *load_config()["agents"]})


def agent_info(name: str, cfg: dict) -> dict:
    _, provider, model = agent_defaults(name)
    custom = cfg["agents"].get(name) or {}
    return {"name": name, "provider": provider, "model": model, "custom": bool(custom),
            "preset": custom.get("model"), "system": custom.get("system", ""), "tools": custom.get("tools", [])}


def workspaces() -> list:
    return sorted(p.name for p in WORKSPACES.glob("*") if p.is_dir()) if WORKSPACES.is_dir() else []


def key_names() -> list:
    return sorted({c["key_env"] for c in PROVIDERS.values()}) + S3_ENV


def view(a) -> dict:
    return {"name": a.name, "provider": a.provider, "model": a.model, "workspace": a.workspace, "busy": a.busy, "error": a.error,
            "messages": to_openai_messages(list(a.messages))}  # copy: a worker thread may be appending


def send(a, text: str) -> None:
    a.busy = True
    try:
        ask(a, text)
    finally:
        a.busy = False


def bad(msg: str):
    raise HTTPException(400, msg)


# --- login (public) ---

@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.post("/api/login", dependencies=[Depends(json_only)])
def login(body: Login, response: Response) -> dict:
    if not secrets.compare_digest(body.password.encode(), PASSWORD.encode()):
        time.sleep(1)  # ponytail: per-request delay only; add per-IP lockout if exposed to the internet
        raise HTTPException(401, "비밀번호가 틀렸습니다")
    response.set_cookie("panel_session", SESSION, httponly=True, samesite="strict", max_age=30 * 86400)
    return {"ok": True}


@app.post("/api/logout", dependencies=[Depends(json_only)])
def logout(response: Response) -> dict:
    response.delete_cookie("panel_session")
    return {"ok": True}


# --- pages ---

PAGES = {"/": "index.html", "/agents": "agents.html", "/models": "models.html",
         "/workspaces": "workspaces.html", "/settings": "settings.html"}


def page(template: str):
    def render(request: Request):
        return templates.TemplateResponse(request, template, {"path": request.url.path})
    return render


for _path, _template in PAGES.items():
    api.add_api_route(_path, page(_template), methods=["GET"], include_in_schema=False)


# --- state ---

@api.get("/api/state")
def state() -> dict:
    with lock:
        agents = list(running.values())
    cfg = load_config()
    return {"available": [agent_info(n, cfg) for n in available()], "running": [view(a) for a in agents],
            "workspaces": [{"name": w, "files": sum(1 for _ in (WORKSPACES / w).iterdir()),
                            "agents": [a.name for a in agents if a.workspace == w]} for w in workspaces()],
            "models": [{"name": n, **m} for n, m in sorted(cfg["models"].items())],
            "providers": {n: {"default": p["model"], "key": p["key_env"]} for n, p in PROVIDERS.items()},
            "tools": list(TOOL_BY_NAME),
            "keys": {k: bool(os.environ.get(k)) for k in key_names()},  # set/unset only, never the value
            "s3": s3_target()}


@api.get("/api/models")
def provider_models(provider: str) -> list:
    if provider not in PROVIDERS:
        bad(f"unknown provider: {provider}")
    return list_models(provider)


# --- models ---

@api.post("/api/models", dependencies=[Depends(json_only)])
def save_model(body: Model) -> dict:
    name, model = body.name.strip(), body.model.strip()
    if not valid_name(name):
        bad("이름은 글자·숫자·- _ . 만")
    if body.provider not in PROVIDERS:
        bad(f"unknown provider: {body.provider}")
    if not model:
        bad("모델 ID를 입력하세요")

    def change(cfg):
        cfg["models"][name] = {"provider": body.provider, "model": model}
    update_config(change)
    return {"ok": True}


@api.post("/api/models/delete", dependencies=[Depends(json_only)])
def delete_model(body: Name) -> dict:
    def change(cfg):
        users = [a for a, d in cfg["agents"].items() if d["model"] == body.name]
        if users:
            bad(f"사용 중인 에이전트가 있습니다: {', '.join(users)}")
        cfg["models"].pop(body.name, None)
    update_config(change)
    return {"ok": True}


# --- agents ---

@api.post("/api/agents", dependencies=[Depends(json_only)])
def save_agent(body: Agent) -> dict:
    name = body.name.strip()
    if not valid_name(name):
        bad("이름은 글자·숫자·- _ . 만")
    if name in file_agents():
        bad(f"agents/{name}.py 와 이름이 겹칩니다")
    if unknown := set(body.tools) - set(TOOL_BY_NAME):
        bad(f"unknown tools: {', '.join(sorted(unknown))}")

    def change(cfg):
        m = cfg["models"].get(body.model)
        if not m:
            bad("모델을 고르세요")
        cfg["agents"][name] = {"model": body.model, "system": body.system.strip(), "tools": body.tools}
    update_config(change)
    return {"ok": True}  # a running copy keeps its old definition until restarted


@api.post("/api/agents/delete", dependencies=[Depends(json_only)])
def delete_agent(body: Name) -> dict:
    update_config(lambda cfg: cfg["agents"].pop(body.name, None))
    return {"ok": True}


# --- workspaces ---

@api.post("/api/workspaces", dependencies=[Depends(json_only)])
def create_workspace(body: Name) -> dict:
    name = body.name.strip()
    if not valid_name(name):
        bad("이름은 글자·숫자·- _ . 만")
    if (WORKSPACES / name).exists():
        bad("이미 있는 워크스페이스입니다")
    workspace_dir(name)
    return {"ok": True}


@api.post("/api/workspaces/delete", dependencies=[Depends(json_only)])
def delete_workspace(body: Name) -> dict:
    if body.name not in workspaces():
        bad("없는 워크스페이스입니다")
    with lock:
        if any(a.workspace == body.name for a in running.values()):
            bad("이 워크스페이스에서 실행 중인 에이전트를 먼저 멈추세요")
        shutil.rmtree(WORKSPACES / body.name)
    return {"ok": True}


# --- running agents ---

@api.post("/api/start", dependencies=[Depends(json_only)])
def start(body: Start) -> dict:
    if body.workspace not in workspaces():
        bad("워크스페이스를 고르세요")
    provider = model = None
    if body.model:
        m = load_config()["models"].get(body.model) or bad(f"unknown model: {body.model}")
        provider, model = m["provider"], m["model"]
    with lock:
        if body.name not in available():
            bad(f"unknown agent: {body.name}")
        if body.name in running:
            bad(f"{body.name} already running")
        try:
            agent = load_agent(body.name, model=model, workspace=body.workspace, provider=provider)
        except BaseException as e:  # load_agent uses SystemExit for missing keys/models
            bad(str(e))
        agent.busy = False
        running[body.name] = agent
    return {"ok": True}


# ponytail: keys live in this process's env only (lost on restart); not written to disk because
# agents' file tools can read the working directory. Running agents keep their old key until restarted.
@api.post("/api/key", dependencies=[Depends(json_only)])
def set_key(body: Key) -> dict:
    if body.env not in key_names():
        bad(f"unknown key: {body.env}")
    value = body.value.strip()
    if value:
        os.environ[body.env] = value
    else:
        os.environ.pop(body.env, None)
    return {"ok": True}


@api.post("/api/s3/upload", dependencies=[Depends(json_only)])
def s3_upload() -> dict:
    try:
        keys = upload_trajectories()
    except BaseException as e:  # missing bucket (SystemExit), bad credentials, network
        bad(f"S3 업로드 실패: {e}")
    return {"ok": True, "count": len(keys), "target": s3_target()}


@api.post("/api/stop", dependencies=[Depends(json_only)])
def stop(body: Name) -> dict:
    with lock:
        running.pop(body.name, None)  # an in-flight turn finishes in the background
    return {"ok": True}


@api.post("/api/reset", dependencies=[Depends(json_only)])
def reset(body: Name) -> dict:
    with lock:
        if body.name in running and not running[body.name].busy:
            running[body.name].messages.clear()
    return {"ok": True}


@api.post("/api/send", dependencies=[Depends(json_only)])
def send_message(body: Message) -> dict:
    text = body.text.strip()
    with lock:
        targets = [running[n] for n in body.names if n in running and not running[n].busy]
        if not text or not targets:
            bad("need text and at least one idle running agent")
        for a in targets:
            a.busy = True  # mark now so a quick second send can't double-start the agent
            threading.Thread(target=send, args=(a, text), daemon=True).start()
    return {"ok": True}


app.include_router(api)

if __name__ == "__main__":
    import uvicorn

    workspace_dir("default")  # so the start dialog always has a workspace
    # Agents can read/write files in their workspace; the password is the only gate, over plain HTTP.
    host = os.environ.get("HOST", "0.0.0.0")  # HOST=127.0.0.1 for this machine only
    print(f"Agent panel: http://127.0.0.1:8000 (listening on {host}:8000)")
    if not os.environ.get("PANEL_PASSWORD"):
        print(f"Password: {PASSWORD}  (set PANEL_PASSWORD to choose one)")
    uvicorn.run(app, host=host, port=8000, log_level="warning")
