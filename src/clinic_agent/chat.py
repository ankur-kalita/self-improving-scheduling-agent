"""Interactive chat with the scheduling agent.

    uv run chat                 # latest policy
    uv run chat --policy 1      # a specific version
    uv run chat --debug         # also print tool calls
"""

from __future__ import annotations

import argparse
import json

from .agent import Agent
from .clinic.db import Clinic
from .llm import LLM
from .policy import AgentPolicy

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def main() -> None:
    ap = argparse.ArgumentParser(description="Chat with the clinic scheduling agent")
    ap.add_argument("--policy", default="latest", help="policy version number or 'latest'")
    ap.add_argument("--debug", action="store_true", help="show tool calls and results")
    args = ap.parse_args()

    llm = LLM()
    policy = AgentPolicy.load_version(args.policy)
    clinic = Clinic()
    agent = Agent(clinic, policy, llm, llm.cfg.agent_model)
    print(f"{BOLD}Maple Grove Family Clinic{RESET} - scheduling assistant (policy v{policy.version}, {llm.cfg.agent_model})")
    print(f"{DIM}Clinic clock: {clinic.now:%A %Y-%m-%d %H:%M}. Try: Jane Doe, DOB 1985-03-14. Ctrl-C or 'quit' to exit.{RESET}\n")

    seen = 0
    while True:
        try:
            user = input(f"{BOLD}you>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user.lower() in {"quit", "exit"}:
            break
        if not user:
            continue
        reply = agent.respond(user)
        if args.debug:
            for ev in agent.tools.events[seen:]:
                print(f"{DIM}  [tool] {ev.name}({json.dumps(ev.args)}) -> {json.dumps(ev.result, default=str)[:300]}{RESET}")
            seen = len(agent.tools.events)
        print(f"{BOLD}agent>{RESET} {reply}\n")


if __name__ == "__main__":
    main()
