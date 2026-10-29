"""Shared agent runtime: tools, providers, and the per-provider tool loops."""
import contextvars
import importlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import openai
from anthropic import beta_tool

WORKSPACES = Path.cwd().resolve() / "workspaces"  # one folder per workspace; agents never see outside theirs
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


TOOLS = [get_time, list_files, read_file, write_file]

# Anthropic server-side tools: run on Anthropic's servers, no local code. Only for PROVIDER = "anthropic".
# web_fetch only fetches URLs already in the conversation (from the user or a web_search result).
WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
]
_SERVER_TOOL_PARAMS = {"web_search": {"query": "string"}, "web_fetch": {"url": "string"}}  # for trajectory schemas
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


def save_trajectory(agent) -> None:
    """Rewrite trajectories/<session>-<agent>.json with the whole conversation in OpenAI fine-tuning format."""
    LOG_DIR.mkdir(exist_ok=True)
    record = {
        "id": f"{SESSION}-{agent.name}", "agent": agent.name, "provider": agent.provider, "model": agent.model,
        "tools": openai_tools(agent.tools),
        "messages": [{"role": "system", "content": agent.system}, *to_openai_messages(agent.messages)],
    }
    (LOG_DIR / f"{record['id']}.json").write_text(json.dumps(record, ensure_ascii=False, default=str))


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
        resp = agent.client.chat.completions.create(
            model=agent.model, messages=[{"role": "system", "content": agent.system}, *messages], tools=schemas,
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
    cfg = PROVIDERS[name]
    key = os.environ.get(cfg["key_env"])
    if not key:
        raise SystemExit(f"set {cfg['key_env']} for provider '{name}'")
    if cfg["kind"] == "openai":
        return openai.OpenAI(api_key=key, base_url=cfg["base_url"]), run_turn_openai, {}
    if cfg["base_url"]:  # third-party Anthropic-compatible endpoint: Bearer auth, no Anthropic-only params
        return anthropic.Anthropic(auth_token=key, base_url=cfg["base_url"]), run_turn_anthropic, {}
    extra = dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    return anthropic.Anthropic(api_key=key), run_turn_anthropic, extra


def workspace_dir(name: str) -> Path:
    """workspaces/<name>, created if missing. Plain names only, so workspaces can't nest or overlap."""
    if not re.fullmatch(r"[\w-][\w.-]*", name):
        raise SystemExit(f"bad workspace name: {name!r} (letters, digits, - _ . only)")
    p = WORKSPACES / name
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def agent_defaults(name: str):
    """agents/<name>.py module, its provider, and its default model (None if it must be chosen)."""
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


def load_agent(name: str, model: str | None = None, workspace: str | None = None):
    """Build a runnable agent from agents/<name>.py (PROVIDER, MODEL, SYSTEM, TOOLS; all optional).

    model overrides the file's MODEL; workspace confines its file tools to workspaces/<workspace> (default: cwd).
    """
    mod, provider, default_model = agent_defaults(name)
    model = model or default_model
    if not model:
        raise SystemExit(f"agents/{name}.py: set MODEL (provider '{provider}' has no default)")
    tools = getattr(mod, "TOOLS", TOOLS)
    if any(isinstance(t, dict) for t in tools) and provider != "anthropic":
        raise SystemExit(f"agents/{name}.py: WEB_TOOLS need PROVIDER = 'anthropic' (got '{provider}')")
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
    save_trajectory(agent)
