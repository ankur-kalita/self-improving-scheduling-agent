"""Eval CLI.

    uv run eval loop                      # baseline -> improve -> re-run -> gate (the main command)
    uv run eval run --policy 1            # score one policy version
    uv run eval run --scenario slot_race  # a single scenario
"""

from __future__ import annotations

import argparse
from datetime import datetime

from clinic_agent.llm import LLM
from clinic_agent.policy import AgentPolicy

from .loop import run_loop
from .runner import ROOT, load_scenarios, print_summary, run_suite, save_run, summarize


def main() -> None:
    ap = argparse.ArgumentParser(prog="eval")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="evaluate one policy version")
    r.add_argument("--policy", default="latest")
    r.add_argument("--split", default="all", choices=["all", "train", "holdout"])
    r.add_argument("--scenario", action="append", help="limit to scenario id (repeatable)")
    r.add_argument("--trials", type=int, default=1)

    lp = sub.add_parser("loop", help="run the self-improvement loop")
    lp.add_argument("--iterations", type=int, default=2)
    lp.add_argument("--trials", type=int, default=1)
    lp.add_argument("--policy", default="latest")
    lp.add_argument("--baseline", help="reuse a saved run directory as the baseline")

    args = ap.parse_args()
    if args.cmd == "loop":
        run_loop(args.iterations, args.trials, args.policy, args.baseline)
        return

    llm = LLM()
    policy = AgentPolicy.load_version(args.policy)
    scenarios = load_scenarios(args.split, args.scenario)
    results = run_suite(policy, llm, scenarios, args.trials)
    summary = summarize(results)
    out = ROOT / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-v{policy.version}"
    save_run(out, policy, results, summary)
    print_summary(summary, f"policy v{policy.version}")
    print(f"\nSaved to {out.relative_to(ROOT)} ({llm.calls} LLM calls, {llm.cache_hits} cache hits)")


if __name__ == "__main__":
    main()
