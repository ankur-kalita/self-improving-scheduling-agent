"""Run scenarios against a policy and score them."""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import yaml

from clinic_agent.agent import Agent, Turn
from clinic_agent.clinic.db import Clinic
from clinic_agent.llm import LLM
from clinic_agent.policy import AgentPolicy

from .checks import global_checks, run_check
from .judge import GLOBAL_RUBRIC, judge
from .patient_sim import PatientSimulator

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_FILE = ROOT / "scenarios" / "scenarios.yaml"
MAX_PATIENT_TURNS = 10


def load_scenarios(split: str | None = None, ids: list[str] | None = None) -> list[dict]:
    scenarios = yaml.safe_load(SCENARIO_FILE.read_text())
    if split and split != "all":
        scenarios = [s for s in scenarios if s["split"] == split]
    if ids:
        scenarios = [s for s in scenarios if s["id"] in ids]
    return scenarios


def run_scenario(scn: dict, policy: AgentPolicy, llm: LLM, trial: int = 0) -> dict:
    salt = f"t{trial}"
    clinic = Clinic()
    agent = Agent(clinic, policy, llm, llm.cfg.agent_model, faults=scn.get("faults"), cache_salt=salt)
    sim = PatientSimulator(llm, scn["persona"], salt=salt)
    t0 = time.monotonic()

    msg, done, ended_by = sim.opening(), False, "max_turns"
    for _ in range(MAX_PATIENT_TURNS):
        agent.respond(msg)
        msg, done = sim.next_message(agent.transcript)
        if done:
            ended_by = "patient_end"
            if msg:
                agent.transcript.append(Turn("patient", msg))
            break

    checks = [run_check(spec, agent, i) for i, spec in enumerate(scn["expect"]) if spec["kind"] != "judge"]
    checks += global_checks(agent)
    rubric = [s for s in scn["expect"] if s["kind"] == "judge"] + GLOBAL_RUBRIC
    checks += judge(llm, agent.transcript, rubric, salt=salt)

    weight = lambda c: 2 if c.critical else 1  # noqa: E731
    score = sum(weight(c) for c in checks if c.passed) / sum(weight(c) for c in checks)
    passed = all(c.passed for c in checks if c.critical)
    return {
        "scenario": scn["id"],
        "split": scn["split"],
        "title": scn["title"],
        "trial": trial,
        "passed": passed,
        "score": round(score, 3),
        "checks": [c.to_dict() for c in checks],
        "ended_by": ended_by,
        "transcript": [asdict(t) for t in agent.transcript],
        "tool_events": [asdict(e) for e in agent.tools.events],
        "errors": agent.errors,
        "seconds": round(time.monotonic() - t0, 1),
    }


def run_suite(policy: AgentPolicy, llm: LLM, scenarios: list[dict], trials: int = 1,
              trial_offset: int = 0, label: str = "") -> list[dict]:
    jobs = [(s, t) for s in scenarios for t in range(trial_offset, trial_offset + trials)]
    workers = int(os.getenv("EVAL_CONCURRENCY", "3"))

    def job(st):
        s, t = st
        try:
            r = run_scenario(s, policy, llm, t)
        except Exception as e:
            r = {"scenario": s["id"], "split": s["split"], "title": s["title"], "trial": t, "passed": False,
                 "score": 0.0, "checks": [{"id": "harness_error", "kind": "harness", "critical": True,
                                           "passed": False, "detail": f"{type(e).__name__}: {e}"}],
                 "ended_by": "error", "transcript": [], "tool_events": [], "errors": [str(e)], "seconds": 0}
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{label}v{policy.version}] {mark} {r['scenario']} (t{t}) score={r['score']:.2f} {r['seconds']}s",
              file=sys.stderr, flush=True)
        return r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(job, jobs))
    return sorted(results, key=lambda r: (r["split"] != "train", r["scenario"], r["trial"]))


def summarize(results: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for r in results:
        by.setdefault(r["scenario"], []).append(r)
    per = {
        sid: {
            "split": rs[0]["split"],
            "title": rs[0]["title"],
            "trials": len(rs),
            "pass_rate": round(sum(r["passed"] for r in rs) / len(rs), 3),
            "mean_score": round(sum(r["score"] for r in rs) / len(rs), 3),
            "failed_checks": sorted({c["id"] for r in rs for c in r["checks"] if not c["passed"]}),
        }
        for sid, rs in by.items()
    }

    def agg(split):
        vals = [v for v in per.values() if split == "all" or v["split"] == split]
        if not vals:
            return {"scenarios": 0, "pass_rate": 0, "mean_score": 0}
        return {
            "scenarios": len(vals),
            "pass_rate": round(sum(v["pass_rate"] for v in vals) / len(vals), 3),
            "mean_score": round(sum(v["mean_score"] for v in vals) / len(vals), 3),
        }

    return {"overall": agg("all"), "train": agg("train"), "holdout": agg("holdout"), "scenarios": per}


def save_run(run_dir: Path, policy: AgentPolicy, results: list[dict], summary: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(json.dumps(results, indent=2, default=str))
    (run_dir / "summary.json").write_text(json.dumps({"policy_version": policy.version, **summary}, indent=2))
    tdir = run_dir / "transcripts"
    tdir.mkdir(exist_ok=True)
    for r in results:
        (tdir / f"{r['scenario']}-t{r['trial']}.md").write_text(render_transcript(r))


def render_transcript(r: dict) -> str:
    lines = [f"# {r['scenario']} (trial {r['trial']}) - {'PASS' if r['passed'] else 'FAIL'} score {r['score']}",
             f"_{r['title']}_", "", "## Conversation"]
    events_by_turn: dict[int, list[dict]] = {}
    for e in r["tool_events"]:
        events_by_turn.setdefault(e["turn"], []).append(e)
    turn = 0
    for t in r["transcript"]:
        if t["role"] == "patient":
            turn += 1
            lines.append(f"\n**Patient:** {t['text']}")
            for e in events_by_turn.get(turn, []):
                res = json.dumps(e["result"], default=str)
                lines.append(f"> `{e['name']}({json.dumps(e['args'])})` -> `{res[:400]}`")
        else:
            guard = f" _(output guard: {t['guard']})_" if t.get("guard") else ""
            lines.append(f"\n**Agent:** {t['text']}{guard}")
    lines += ["", "## Checks", "| check | kind | critical | result | detail |", "|---|---|---|---|---|"]
    for c in r["checks"]:
        detail = str(c["detail"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {c['id']} | {c['kind']} | {'yes' if c['critical'] else ''} | "
                     f"{'pass' if c['passed'] else '**FAIL**'} | {detail} |")
    return "\n".join(lines) + "\n"


def print_summary(summary: dict, title: str = "") -> None:
    if title:
        print(f"\n== {title} ==")
    for split in ("train", "holdout", "overall"):
        s = summary[split]
        print(f"  {split:8s} pass_rate={s['pass_rate']:.2f} mean_score={s['mean_score']:.3f} ({s['scenarios']} scenarios)")
    for sid, v in summary["scenarios"].items():
        fails = ", ".join(v["failed_checks"]) or "-"
        print(f"  {v['split']:7s} {sid:24s} pass={v['pass_rate']:.2f} score={v['mean_score']:.2f}  failed: {fails}")
