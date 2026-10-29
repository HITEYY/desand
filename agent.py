"""Run one or more agents (agents/<name>.py) in parallel, each with its own memory.

  python agent.py                         # chat with agents/default.py
  python agent.py -a researcher -a writer # every message goes to both, in parallel
  python agent.py -a researcher -a writer "summarize README"   # one-shot
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core import ask, load_agent


def broadcast(agents, text: str) -> None:
    with ThreadPoolExecutor(len(agents)) as pool:
        list(pool.map(lambda a: ask(a, text), agents))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-a", "--agent", action="append", help="agent file name in agents/ (repeatable)")
    ap.add_argument("--list", action="store_true", help="list available agents")
    ap.add_argument("prompt", nargs="?", help="run once with this prompt instead of chatting")
    args = ap.parse_args()

    if args.list:
        print("\n".join(sorted(p.stem for p in Path("agents").glob("*.py") if p.stem != "__init__")))
        return
    agents = [load_agent(n) for n in dict.fromkeys(args.agent or ["default"])]
    if args.prompt:
        broadcast(agents, args.prompt)
        return

    names = ", ".join(f"{a.name}({a.provider}/{a.model})" for a in agents)
    print(f"Agents: {names}. 'exit' to quit, '/reset' to clear memory.")
    while True:
        try:
            user = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if user in ("exit", "quit"):
            break
        if user == "/reset":
            for a in agents:
                a.messages.clear()
            print("(memory cleared)")
            continue
        if user:
            broadcast(agents, user)


if __name__ == "__main__":
    main()
