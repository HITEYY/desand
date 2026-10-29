"""Shared agent runtime: tools, providers, and the per-provider tool loops."""
import contextvars
import html
import importlib
import json
import os
import re
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import openai
from anthropic import beta_tool

WORKSPACES = Path.cwd().resolve() / "workspaces"  # one folder per workspace; agents never see outside theirs
# Models and agents created in the web panel. Next to workspaces/, not inside, so panel agents can't read it.
CONFIG = Path.cwd().resolve() / "panel.json"
_config_lock = threading.Lock()
# Working dir for file tools, set per agent turn in ask() (tools run on the agent's own thread). CLI: cwd.
_root = contextvars.ContextVar("root", default=Path.cwd().resolve())
LOG_DIR = Path(__file__).parent / "trajectories"
SESSION = datetime.now().strftime("%Y%m%d-%H%M%S")

# kind: which wire API the provider speaks. model=None -> pass --model (startup lists available ids).
PROVIDERS = {
    "anthropic": dict(kind="anthropic", base_url=None, key_env="ANTHROPIC_API_KEY", model="claude-opus-5-5"),
    "opencode-go": dict(kind="openai", base_url="https://opencode.ai/inference/go/openai/v1",
                        key_env="OPENCODE_API_KEY", model="kimi-k3"),
    # opencode-go models served on its Anthropic-compatible endpoint (claude-haiku-5-5, qwen3.8-max, minimax-m3, ...)
    "opencode-go-anthropic": dict(kind="anthropic", base_url="https://opencode.ai/inference/go/anthropic",
                                  key_env="OPENCODE_API_KEY", model="claude-haiku-5-5"),
    "openscale": dict(kind="openai", base_url="https://api.openscale.so/v1", key_env="OPENSCALE_KEY", model=None),
    "experiential": dict(kind="openai", base_url="https://api.experientiallabs.ai/v1",
                         key_env="EXPERIENTIAL_API_KEY", model=None),
    # Hinoki (app.hinoki.security): same host also speaks Anthropic Messages at /v1/messages
    "hinoki": dict(kind="openai", base_url="https://inference.hinoki.security/v1", key_env="HINOKI_API_KEY", model=None),
}


def _safe(path: str) -> Path:
    root = _root.get()
    p = (root / path).resolve()  # resolve() follows symlinks, so a link can't escape the workspace either
    if not p.is_relative_to(root):
        raise ValueError(f"path outside working dir: {path}")
    return p


@beta_tool
def get_time() -> str:
    """Get the current local date and time."""
    return datetime.now().isoformat(timespec="seconds")


@beta_tool
def list_files(path: str = ".") -> str:
    """List files in a directory inside the working directory.

    Args:
        path: Directory path relative to the working directory.
    """
    return "\n".join(sorted(f.name + ("/" if f.is_dir() else "") for f in _safe(path).iterdir()))


@beta_tool
def read_file(path: str) -> str:
    """Read a text file inside the working directory.

    Args:
        path: File path relative to the working directory.
    """
    return _safe(path).read_text(errors="replace")[:50_000]


@beta_tool
def write_file(path: str, content: str) -> str:
    """Write text to a file inside the working directory (overwrites).

    Args:
        path: File path relative to the working directory.
        content: Full text content to write.
    """
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} chars to {path}"


# Playwright's sync API is bound to the thread that started it, so each agent turn (one thread) gets
# its own browser, launched on first use and closed when the turn ends.
_browser = threading.local()


def _page():
    page = getattr(_browser, "page", None)
    if page is None:
        from playwright.sync_api import sync_playwright  # imported lazily so the app runs without a browser
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=True)
        except BaseException:
            pw.stop()
            raise
        _browser.playwright, _browser.browser, _browser.page = pw, browser, browser.new_page()
        page = _browser.page
    return page


def close_browser() -> None:
    """Close this thread's browser (if any); called after each agent turn."""
    browser = getattr(_browser, "browser", None)
    if browser is not None:
        browser.close()
        _browser.playwright.stop()
        _browser.page = _browser.browser = _browser.playwright = None


def _snapshot(page) -> str:
    return f"URL: {page.url}\nTitle: {page.title()}\n\n{page.inner_text('body')[:20_000]}"


def _settle(page) -> None:
    try:  # pages with long-polling requests may never finish loading; the text is still usable
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass


@beta_tool
def browser_open(url: str) -> str:
    """Open a URL in a headless browser and return the page text.

    Args:
        url: The page URL to open (http/https).
    """
    page = _page()
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    return _snapshot(page)


@beta_tool
def browser_click(selector: str) -> str:
    """Click an element on the page that browser_open opened, then return the page text.

    Args:
        selector: CSS selector of the element to click (e.g. "button#submit" or "a[href*=next]").
    """
    page = _page()
    page.click(selector, timeout=10_000)
    _settle(page)
    return _snapshot(page)


@beta_tool
def browser_fill(selector: str, text: str) -> str:
    """Type text into an input on the page that browser_open opened (does not submit).

    Args:
        selector: CSS selector of the input or textarea.
        text: Text to enter.
    """
    page = _page()
    page.fill(selector, text, timeout=10_000)
    return f"filled {selector} with {len(text)} chars"


@beta_tool
def browser_text() -> str:
    """Return the text of the page that browser_open opened."""
    return _snapshot(_page())


@beta_tool
def browser_screenshot(path: str = "screenshot.png") -> str:
    """Save a full-page PNG screenshot of the open page inside the working directory.

    Args:
        path: File path relative to the working directory.
    """
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _page().screenshot(path=str(p), full_page=True)
    return f"saved screenshot to {path}"


def _get(url: str, data: bytes | None = None) -> str:
    req = urllib.request.Request(url, data, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(2_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")


def _strip_tags(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>|<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# Local stand-ins for Anthropic's server tools (same names), used by every provider except native "anthropic".
@beta_tool
def local_web_search(query: str) -> str:
    """Search the web and return the top results (title, URL, snippet).

    Args:
        query: Search query.
    """
    # ponytail: scrapes DuckDuckGo's HTML page; swap for a search API if it starts rate-limiting
    page = _get("https://html.duckduckgo.com/html/", urllib.parse.urlencode({"q": query}).encode())
    out = []
    for href, title, snippet in re.findall(
            r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', page, re.S)[:8]:
        url = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query).get("uddg", [href])[0]
        out.append(f"{_strip_tags(title)}\n{url}\n{_strip_tags(snippet)}")
    return "\n\n".join(out) or "no results"


@beta_tool
def local_web_fetch(url: str) -> str:
    """Fetch a web page (http/https) and return its text.

    Args:
        url: The page URL.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("only http/https URLs")
    return _strip_tags(_get(url))[:50_000]


local_web_search.name, local_web_fetch.name = "web_search", "web_fetch"


BROWSER_TOOLS = [browser_open, browser_click, browser_fill, browser_text, browser_screenshot]
TOOLS = [get_time, list_files, read_file, write_file, *BROWSER_TOOLS]

# Anthropic server-side tools: run on Anthropic's servers, no local code. On any other provider load_agent swaps
# them for the local web_search / web_fetch above. Server web_fetch only fetches URLs already in the conversation.
WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
]
_SERVER_TOOL_PARAMS = {"web_search": {"query": "string"}, "web_fetch": {"url": "string"}}  # for trajectory schemas
TOOL_BY_NAME = {t["name"] if isinstance(t, dict) else t.name: t for t in [*TOOLS, *WEB_TOOLS]}
DEFAULT_SYSTEM = "You are a helpful assistant. Use the tools when they help. Answer in the user's language."


def openai_tools(tools) -> list:
    out = []
    for t in tools:
        if isinstance(t, dict):  # server tool: describe it as a function so training data stays one format
            props = {k: {"type": v} for k, v in _SERVER_TOOL_PARAMS.get(t["name"], {}).items()}
            d = {"name": t["name"], "description": f"Anthropic server tool {t['type']}",
                 "input_schema": {"type": "object", "properties": props, "required": list(props)}}
        else:
            d = t.to_dict()
        out.append({"type": "function", "function": {"name": d["name"], "description": d["description"],
                                                      "parameters": d["input_schema"]}})
    return out


def to_openai_messages(messages: list) -> list:
    """Normalize Anthropic-format history to OpenAI chat format (thinking blocks dropped)."""
    out = []
    for m in messages:
        content = m.get("content")  # OpenAI tool-call messages may omit it
        if isinstance(content, str) or m["role"] == "tool" or "tool_calls" in m:
            out.append(m)  # already OpenAI-shaped
            continue
        blocks = [b if isinstance(b, dict) else b.model_dump(exclude_none=True) for b in content]
        if m["role"] == "assistant":
            # Server tool results sit inside the assistant turn; split there so each call is followed by its result.
            text, calls = "", []
            for b in blocks + [None]:
                if b is None or b["type"].endswith("_tool_result"):
                    if text or calls:
                        out.append({"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})})
                    if b is not None:
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                    "content": json.dumps(b.get("content"), ensure_ascii=False, default=str)[:20_000]})
                    text, calls = "", []
                elif b["type"] == "text":
                    text += b["text"]
                elif b["type"] in ("tool_use", "server_tool_use"):
                    calls.append({"id": b["id"], "type": "function",
                                  "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}})
            continue
        for b in blocks:
            if b["type"] == "tool_result":
                c = b.get("content", "")
                if isinstance(c, list):
                    c = "".join(x.get("text", "") for x in c)
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": c})
            elif b["type"] == "text":
                out.append({"role": "user", "content": b["text"]})
    return out


def finetune_message(m: dict) -> dict:
    """Keep only the keys OpenAI fine-tuning accepts; providers add extras (reasoning_content, refusal, annotations...)."""
    out = {"role": m["role"]}
    if m.get("content") is not None:
        out["content"] = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], ensure_ascii=False)
    if m.get("tool_calls"):
        out["tool_calls"] = [{"id": c["id"], "type": "function",
                              "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}}
                             for c in m["tool_calls"]]
    if m["role"] == "tool":
        out["tool_call_id"] = m["tool_call_id"]
    return out


def save_trajectory(agent) -> None:
    """Rewrite trajectories/<session>-<agent>.jsonl: one OpenAI fine-tuning example ({"messages", "tools"}) per file."""
    LOG_DIR.mkdir(exist_ok=True)
    messages = [{"role": "system", "content": agent.system}, *to_openai_messages(agent.messages)]
    record = {"messages": [finetune_message(m) for m in messages]}
    if agent.tools:
        record["tools"] = openai_tools(agent.tools)
    path = LOG_DIR / f"{SESSION}-{agent.name}.jsonl"
    tmp = path.with_suffix(".tmp")  # write then rename, so an S3 upload never reads a half-written file
    tmp.write_text(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


# S3 settings, read from env (the panel's key page sets them too). Credentials also come from boto3's usual
# sources (~/.aws, IAM role) when the AWS_* vars are unset. S3_ENDPOINT_URL is for R2 / MinIO / other S3-compatibles.
S3_ENV = ["S3_BUCKET", "S3_PREFIX", "S3_ENDPOINT_URL", "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]


def s3_target() -> str | None:
    bucket = os.environ.get("S3_BUCKET")
    prefix = os.environ.get("S3_PREFIX", "trajectories").strip("/")
    return f"s3://{bucket}/{prefix + '/' if prefix else ''}" if bucket else None


def upload_trajectories() -> list:
    """Upload every trajectories/*.jsonl to s3://$S3_BUCKET/$S3_PREFIX/ (overwrites); returns the uploaded keys."""
    target = s3_target()
    if not target:
        raise SystemExit("set S3_BUCKET")
    import boto3  # lazy, like playwright: only needed when uploading
    bucket, prefix = target.removeprefix("s3://").split("/", 1)
    s3 = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
                      region_name=os.environ.get("AWS_REGION") or None)
    keys = []
    # ponytail: re-uploads every file each time; skip unchanged ones (compare size/ETag) if the folder gets big
    for f in sorted(LOG_DIR.glob("*.jsonl")):
        s3.upload_file(str(f), bucket, prefix + f.name, ExtraArgs={"ContentType": "application/jsonl"})
        keys.append(prefix + f.name)
    return keys


def run_turn_anthropic(agent, messages: list) -> None:
    runner = agent.client.beta.messages.tool_runner(
        model=agent.model, max_tokens=16000, system=agent.system, tools=agent.tools, messages=messages,
        **agent.extra,
    )
    paused = False
    for message in runner:
        # Runner keeps its own history; mirror it so memory survives across turns.
        if paused:  # runner resumed a server-tool pause_turn: same assistant turn, keep it one message
            messages[-1]["content"] = [*messages[-1]["content"], *message.content]
        else:
            messages.append({"role": "assistant", "content": message.content})
        paused = message.stop_reason == "pause_turn"
        for block in message.content:
            if block.type == "text":
                print(f"\n[{agent.name}] {block.text}")
            elif block.type in ("tool_use", "server_tool_use"):
                print(f"  [{agent.name}:tool] {block.name}({block.input})")
        if message.stop_reason == "refusal":
            print(f"\n[{agent.name}] (request declined)")
        tool_response = runner.generate_tool_call_response()
        if tool_response is not None:
            messages.append(tool_response)


def run_turn_openai(agent, messages: list) -> None:
    tool_map = {t.name: t for t in agent.tools}
    schemas = openai_tools(agent.tools)
    while True:
        resp = agent.client.chat.completions.create(  # an empty tools list is rejected, so omit it
            model=agent.model, messages=[{"role": "system", "content": agent.system}, *messages],
            **({"tools": schemas} if schemas else {}),
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if msg.content:
            print(f"\n[{agent.name}] {msg.content}")
        if not msg.tool_calls:
            return
        for tc in msg.tool_calls:
            print(f"  [{agent.name}:tool] {tc.function.name}({tc.function.arguments})")
            try:
                out = tool_map[tc.function.name].call(json.loads(tc.function.arguments or "{}"))
            except Exception as e:  # bad args / unknown tool / path outside workspace -> tell the model
                out = f"error: {e}"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(out)})


def make_client(name: str):
    """One client per agent, so per-client headers identify that agent's conversation."""
    cfg = PROVIDERS[name]
    key = os.environ.get(cfg["key_env"])
    if not key:
        raise SystemExit(f"set {cfg['key_env']} for provider '{name}'")
    headers = {}
    if "opencode.ai" in (cfg["base_url"] or ""):
        # OpenCode Go rejects requests without a stable per-conversation session id and wants a non-SDK user agent.
        headers = {"x-opencode-session": str(uuid.uuid4()), "User-Agent": "desand/1.0"}
    if cfg["kind"] == "openai":
        return openai.OpenAI(api_key=key, base_url=cfg["base_url"], default_headers=headers), run_turn_openai, {}
    if cfg["base_url"]:  # third-party Anthropic-compatible endpoint: Bearer auth, no Anthropic-only params
        return (anthropic.Anthropic(auth_token=key, base_url=cfg["base_url"], default_headers=headers),
                run_turn_anthropic, {})
    extra = dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    return anthropic.Anthropic(api_key=key), run_turn_anthropic, extra


def workspace_dir(name: str) -> Path:
    """workspaces/<name>, created if missing. Plain names only, so workspaces can't nest or overlap."""
    if not valid_name(name):
        raise SystemExit(f"bad workspace name: {name!r} (letters, digits, - _ . only)")
    p = WORKSPACES / name
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[\w-][\w.-]*", name))


def load_config() -> dict:
    """panel.json: {"models": {name: {provider, model}}, "agents": {name: {model, system, tools}}}."""
    try:
        cfg = json.loads(CONFIG.read_text())
    except FileNotFoundError:
        cfg = {}
    return {"models": cfg.get("models", {}), "agents": cfg.get("agents", {})}


def update_config(change) -> None:
    """Read-modify-write panel.json under a lock; change(cfg) mutates cfg or raises to abort."""
    with _config_lock:
        cfg = load_config()
        change(cfg)
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def file_agents() -> list:
    return sorted(p.stem for p in (Path(__file__).parent / "agents").glob("*.py") if p.stem != "__init__")


def agent_defaults(name: str):
    """Agent definition (module-like: SYSTEM, TOOLS), its provider, and default model (None if it must be chosen).

    Panel-made agents (panel.json) point at a saved model; file agents are agents/<name>.py.
    """
    cfg = load_config()
    custom = cfg["agents"].get(name)
    if custom:
        m = cfg["models"].get(custom["model"])
        if not m:
            raise SystemExit(f"agent '{name}': model '{custom['model']}' no longer exists")
        mod = SimpleNamespace(SYSTEM=custom.get("system") or DEFAULT_SYSTEM,
                              TOOLS=[TOOL_BY_NAME[t] for t in custom.get("tools", []) if t in TOOL_BY_NAME])
        return mod, m["provider"], m["model"]
    if name not in file_agents():
        raise SystemExit(f"unknown agent: {name}")
    mod = importlib.import_module(f"agents.{name}")
    provider = getattr(mod, "PROVIDER", "anthropic")
    return mod, provider, getattr(mod, "MODEL", None) or PROVIDERS[provider]["model"]


def list_models(provider: str) -> list:
    """Model ids the provider reports; [] when the key is missing or the endpoint has no listing."""
    try:
        client = make_client(provider)[0]
        return sorted(m.id for m in client.models.list())
    except BaseException:  # make_client raises SystemExit for a missing key
        return []


def load_agent(name: str, model: str | None = None, workspace: str | None = None, provider: str | None = None):
    """Build a runnable agent from agents/<name>.py (PROVIDER, MODEL, SYSTEM, TOOLS; all optional) or panel.json.

    provider/model override the definition's; workspace confines file tools to workspaces/<workspace> (default: cwd).
    """
    mod, default_provider, default_model = agent_defaults(name)
    if provider and provider != default_provider:
        default_model = None  # the definition's model belongs to its own provider
    provider = provider or default_provider
    model = model or default_model
    if not model:
        raise SystemExit(f"agents/{name}.py: set MODEL (provider '{provider}' has no default)")
    tools = getattr(mod, "TOOLS", TOOLS)
    if provider != "anthropic":  # server tools only run on Anthropic's own API
        local = {"web_search": local_web_search, "web_fetch": local_web_fetch}
        tools = [local[t["name"]] if isinstance(t, dict) else t for t in tools]
    client, run, extra = make_client(provider)
    return SimpleNamespace(
        name=name, provider=provider, model=model, client=client, run=run, extra=extra,
        workspace=workspace, root=_root.get() if workspace is None else workspace_dir(workspace),
        system=getattr(mod, "SYSTEM", DEFAULT_SYSTEM), tools=tools, messages=[], error=None,
    )


def ask(agent, text: str) -> None:
    """One user turn against the agent's own memory; failed turns are rolled back."""
    start = len(agent.messages)
    agent.messages.append({"role": "user", "content": text})
    agent.error = None
    _root.set(agent.root)  # only this thread's context: other agents keep their own workspace
    try:
        agent.run(agent, agent.messages)
    except Exception as e:  # one agent failing must not take down the others
        print(f"[{agent.name}] error: {e}")
        agent.error = str(e)
        del agent.messages[start:]
        return
    finally:
        close_browser()
    save_trajectory(agent)
