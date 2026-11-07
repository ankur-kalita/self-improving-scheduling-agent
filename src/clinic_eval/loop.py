"""Close the loop: evaluate -> diagnose -> apply -> re-evaluate -> gate."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from clinic_agent.clinic.db import Clinic
from clinic_agent.llm import LLM
from clinic_agent.policy import AgentPolicy, PolicyError
from clinic_agent.tools import ToolBox

from .improver import propose
from .runner import ROOT, load_scenarios, print_summary, run_suite, save_run, summarize

CONFIRM_TRIALS = 2


def _tool_info(policy: AgentPolicy) -> tuple[list[str], str]:
    tb = ToolBox(Clinic(), policy)
    specs = tb.openai_tools()
    names = [t["function"]["name"] for t in specs]
    desc = "\n".join(f"- {t['function']['name']}: {t['function']['description']}" for t in specs)
    return names, desc


def gate(base: dict, cand: dict, confirm) -> dict:
    """Accept iff train score improves and no scenario's pass rate regresses (after flake confirmation)."""
    suspects = [sid for sid, b in base["scenarios"].items()
                if cand["scenarios"][sid]["pass_rate"] < b["pass_rate"]]
    confirmed, cleared = [], []
    if suspects:
        extra = summarize(confirm(suspects))
        for sid in suspects:
            rate = extra["scenarios"][sid]["pass_rate"]
            (confirmed if rate < base["scenarios"][sid]["pass_rate"] else cleared).append(
                {"scenario": sid, "baseline": base["scenarios"][sid]["pass_rate"],
                 "candidate": cand["scenarios"][sid]["pass_rate"], "confirm_rerun": rate})
    d_train = round(cand["train"]["mean_score"] - base["train"]["mean_score"], 3)
    d_pass = round(cand["overall"]["pass_rate"] - base["overall"]["pass_rate"], 3)
    reasons = []
    if confirmed:
        reasons.append(f"confirmed regressions: {[c['scenario'] for c in confirmed]}")
    if d_train <= 0:
        reasons.append(f"train mean score did not improve ({d_train:+.3f})")
    if d_pass < 0 and not confirmed:
        reasons.append(f"overall pass rate dropped ({d_pass:+.3f})")
    return {"accepted": not reasons, "reasons": reasons, "delta_train_score": d_train,
            "delta_overall_pass_rate": d_pass, "regressions": confirmed, "flakes_cleared": cleared}


def run_loop(iterations: int = 2, trials: int = 1, policy_version: str = "latest",
             baseline_dir: str | None = None) -> Path:
    llm = LLM()
    scenarios = load_scenarios()
    scenario_ids = [s["id"] for s in scenarios]
    policy = AgentPolicy.load_version(policy_version)
    out = ROOT / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-loop"
    out.mkdir(parents=True)

    if baseline_dir:
        results = json.loads((Path(baseline_dir) / "results.json").read_text())
        print(f"Reusing baseline run from {baseline_dir}")
    else:
        print(f"Baseline: policy v{policy.version}, {len(scenarios)} scenarios x {trials} trial(s)")
        results = run_suite(policy, llm, scenarios, trials, label="base ")
    base = summarize(results)
    save_run(out / f"v{policy.version}-baseline", policy, results, base)
    print_summary(base, f"baseline v{policy.version}")
    first_summary, first_version = base, policy.version

    log: list[dict] = []
    rejected: list[dict] = []
    for it in range(1, iterations + 1):
        failures = [r for r in results if r["split"] == "train" and not r["passed"]]
        if not failures:
            print("No train failures left; stopping.")
            break
        names, descs = _tool_info(policy)
        print(f"\n-- iteration {it}: diagnosing {len(failures)} train failure(s) --")
        proposal = propose(llm, policy, failures, names, descs, scenario_ids, rejected)
        entry = {"iteration": it, "from_version": policy.version, "proposal": proposal}
        _print_proposal(proposal)
        if not proposal["changes"]:
            entry["decision"] = {"accepted": False, "reasons": ["no valid changes proposed"]}
            log.append(entry)
            rejected.append({"changes": proposal.get("dropped_changes", []), "reason": "no valid changes"})
            continue
        try:
            cand = policy.apply(proposal["changes"], note=f"loop iteration {it}", tool_names=set(names))
        except PolicyError as e:
            entry["decision"] = {"accepted": False, "reasons": [f"invalid change: {e}"]}
            log.append(entry)
            rejected.append({"changes": proposal["changes"], "reason": str(e)})
            continue

        print(f"Re-running all scenarios with candidate v{cand.version}")
        cand_results = run_suite(cand, llm, scenarios, trials, label="cand ")
        cand_sum = summarize(cand_results)
        save_run(out / f"v{cand.version}-candidate-it{it}", cand, cand_results, cand_sum)
        print_summary(cand_sum, f"candidate v{cand.version}")

        def confirm(ids, _cand=cand):
            print(f"Confirming suspected regressions with {CONFIRM_TRIALS} fresh trials: {ids}")
            return run_suite(_cand, llm, load_scenarios(ids=ids), CONFIRM_TRIALS, trial_offset=100, label="confirm ")

        decision = gate(base, cand_sum, confirm)
        entry["to_version"] = cand.version
        entry["decision"] = decision
        entry["before"] = {k: base[k] for k in ("train", "holdout", "overall")}
        entry["after"] = {k: cand_sum[k] for k in ("train", "holdout", "overall")}
        log.append(entry)
        if decision["accepted"]:
            cand.history[-1]["note"] = (f"loop iteration {it}: train score {decision['delta_train_score']:+.3f}, "
                                        f"pass rate {decision['delta_overall_pass_rate']:+.3f}")
            path = cand.save()
            print(f"ACCEPTED -> {path.relative_to(ROOT)}")
            policy, results, base = cand, cand_results, cand_sum
        else:
            print(f"REJECTED: {'; '.join(decision['reasons'])}")
            rejected.append({"changes": proposal["changes"], "reason": "; ".join(decision["reasons"])})

    (out / "loop_log.json").write_text(json.dumps(log, indent=2, default=str))
    (out / "report.md").write_text(render_report(first_version, first_summary, policy.version, base, log))
    print(f"\nReport: {(out / 'report.md').relative_to(ROOT)}")
    return out


def _print_proposal(p: dict) -> None:
    for d in p.get("diagnoses", []):
        print(f"  diagnosis [{d.get('layer')}] {d.get('scenario')}: {d.get('root_cause')}")
    for c in p["changes"]:
        print(f"  change {c.get('op')} {c.get('target', '')}: {c.get('value')}")
    for c in p.get("dropped_changes", []):
        print(f"  dropped {c.get('op')}: {c.get('dropped_because')}")
    for c in p.get("code_proposals", []):
        print(f"  code proposal: {c.get('title')}")
    for c in p.get("eval_concerns", []):
        print(f"  eval concern {c.get('scenario')}: {c.get('concern')}")


def render_report(v0: int, s0: dict, v1: int, s1: dict, log: list[dict]) -> str:
    L = [f"# Improvement loop report: v{v0} -> v{v1}", ""]
    L += ["| split | pass rate before | after | mean score before | after |", "|---|---|---|---|---|"]
    for k in ("train", "holdout", "overall"):
        L.append(f"| {k} | {s0[k]['pass_rate']:.2f} | {s1[k]['pass_rate']:.2f} | "
                 f"{s0[k]['mean_score']:.3f} | {s1[k]['mean_score']:.3f} |")
    L += ["", "## Per scenario", "| scenario | split | before | after | failed checks after |", "|---|---|---|---|---|"]
    for sid, b in s0["scenarios"].items():
        a = s1["scenarios"][sid]
        L.append(f"| {sid} | {b['split']} | {b['pass_rate']:.2f} ({b['mean_score']:.2f}) | "
                 f"{a['pass_rate']:.2f} ({a['mean_score']:.2f}) | {', '.join(a['failed_checks']) or '-'} |")
    for e in log:
        p = e["proposal"]
        d = e["decision"]
        L += ["", f"## Iteration {e['iteration']} (v{e['from_version']} -> candidate v{e.get('to_version', '-')}): "
                  f"{'ACCEPTED' if d['accepted'] else 'REJECTED'}"]
        if d.get("reasons"):
            L.append(f"Reasons: {'; '.join(d['reasons'])}")
        if "delta_train_score" in d:
            L.append(f"Train score delta {d['delta_train_score']:+.3f}, overall pass-rate delta "
                     f"{d['delta_overall_pass_rate']:+.3f}. Regressions: {d['regressions'] or 'none'}. "
                     f"Flakes cleared by re-run: {d['flakes_cleared'] or 'none'}.")
        L += ["", "Diagnoses:"]
        L += [f"- `{x.get('scenario')}` [{x.get('layer')}]: {x.get('root_cause')}" for x in p.get("diagnoses", [])]
        L += ["", "Applied changes:"]
        L += [f"- `{c.get('op')}` {c.get('target', '')}: {c.get('value')}  \n  _why_: {c.get('rationale')} "
              f"(addresses {c.get('addresses')})" for c in p["changes"]] or ["- none"]
        if p.get("dropped_changes"):
            L += ["", "Dropped by validator:"] + [f"- {c.get('value')} ({c.get('dropped_because')})" for c in p["dropped_changes"]]
        if p.get("code_proposals"):
            L += ["", "Code proposals (for a human):"] + [f"- **{c.get('title')}**: {c.get('description')}" for c in p["code_proposals"]]
        if p.get("eval_concerns"):
            L += ["", "Eval-harness concerns:"] + [f"- `{c.get('scenario')}`: {c.get('concern')}" for c in p["eval_concerns"]]
    return "\n".join(L) + "\n"
