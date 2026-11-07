"""Turn failed runs into a structured, validated policy change.

The improver only sees *train* failures. Its output is a typed proposal:
diagnoses (with the layer the root cause lives in), concrete policy edits,
code-change proposals for humans, and eval-harness concerns (cases where the
simulator or judge, not the agent, was wrong). Proposals are validated
before they are applied, mainly against overfitting to specific scenarios.
"""

from __future__ import annotations

import json
import re

from clinic_agent.clinic.data import PATIENTS
from clinic_agent.llm import LLM, parse_json
from clinic_agent.policy import RUNTIME_BOUNDS, AgentPolicy
from clinic_agent.prompts import CORE_PROMPT

IMPROVER_PROMPT = """\
You are improving a clinic appointment-scheduling agent using failures from its evaluation runs.

The agent = fixed core system prompt (read-only) + an editable policy. You may edit only the policy:
- add_playbook_rule {{"value": "<rule>"}}: a general behavioural rule appended to the system prompt.
- edit_playbook_rule {{"target": "<rule id>", "value": "<new text>"}}
- set_tool_note {{"target": "<tool name>", "value": "<note>"}}: extra usage guidance attached to that tool's description \
(replaces any existing note for that tool).
- set_runtime {{"target": "<key>", "value": <number>}}: runtime knobs with bounds {bounds}.

Anything needing code (new tools, changed tool behaviour, new data) goes in "code_proposals" for a human. \
If a failure was caused by the evaluation harness (simulated patient broke character or ignored its script, \
judge misread the transcript, check is wrong), put it in "eval_concerns" and do NOT change the agent for it.

Requirements for changes:
- Fix root causes, not symptoms. Prefer the layer where the fix belongs: tool guidance for tool-usage mistakes, \
runtime for retry/timeout problems, playbook rules for conversational behaviour.
- Rules must be GENERAL: never mention specific patient names, dates, slot ids, appointment ids or scenario ids. \
They must still make sense for unseen conversations.
- Do not weaken safety or privacy behaviour. Do not duplicate what the core prompt already says unless the failure \
shows the agent ignores it; then make the instruction more specific and operational (when exactly, which tool, what to say).
- Keep it minimal: at most {max_changes} changes. Every change must cite the scenario ids it addresses.

Return JSON:
{{"diagnoses": [{{"scenario": "...", "failed_checks": ["..."], "root_cause": "...", "layer": "prompt|tool_note|runtime|code|eval"}}],
  "changes": [{{"op": "...", "target": "...", "value": "...", "rationale": "...", "addresses": ["scenario_id"]}}],
  "code_proposals": [{{"title": "...", "description": "..."}}],
  "eval_concerns": [{{"scenario": "...", "concern": "..."}}]}}

# Core system prompt (read-only)
{core}

# Tools
{tools}

# Current policy
playbook: {playbook}
tool_notes: {tool_notes}
runtime: {runtime}

{rejected}
# Failures
{failures}
"""


def _failure_digest(r: dict) -> str:
    failed = [c for c in r["checks"] if not c["passed"]]
    lines = [f"## scenario={r['scenario']} trial={r['trial']} ({r['title']})",
             "Failed checks:"]
    lines += [f"- {c['id']} [{c['kind']}{', critical' if c['critical'] else ''}]: {c['detail']}" for c in failed]
    lines.append("Conversation with tool calls:")
    turn = 0
    ev_by_turn: dict[int, list] = {}
    for e in r["tool_events"]:
        ev_by_turn.setdefault(e["turn"], []).append(e)
    for t in r["transcript"]:
        if t["role"] == "patient":
            turn += 1
            lines.append(f"PATIENT: {t['text']}")
            for e in ev_by_turn.get(turn, []):
                lines.append(f"  TOOL {e['name']}({json.dumps(e['args'])}) -> {json.dumps(e['result'], default=str)[:300]}")
        else:
            lines.append(f"AGENT: {t['text']}")
    return "\n".join(lines)


_BANNED = [re.escape(p["name"].lower()) for p in PATIENTS] + [
    r"\b20\d\d-\d\d-\d\d\b", r"\bD0\d-\d{8}", r"\bA\d{4}\b", r"\bP0\d\d\b",
]


def validate_change(ch: dict, scenario_ids: list[str]) -> str | None:
    text = f"{ch.get('target', '')} {ch.get('value', '')}".lower()
    for pat in _BANNED + [re.escape(s.lower()) for s in scenario_ids]:
        if re.search(pat, text):
            return f"overfit: mentions {pat!r}"
    return None


def propose(llm: LLM, policy: AgentPolicy, failures: list[dict], tool_names: list[str], tool_descs: str,
            scenario_ids: list[str], rejected: list[dict], max_changes: int = 3) -> dict:
    rej = ""
    if rejected:
        rej = "# Previously rejected proposals (do not repeat; learn from why they failed)\n" + "\n".join(
            f"- {json.dumps(x['changes'])} -> rejected: {x['reason']}" for x in rejected) + "\n"
    prompt = IMPROVER_PROMPT.format(
        bounds=json.dumps({k: list(v) for k, v in RUNTIME_BOUNDS.items()}),
        max_changes=max_changes,
        core=CORE_PROMPT,
        tools=tool_descs,
        playbook=json.dumps(policy.playbook),
        tool_notes=json.dumps(policy.tool_notes),
        runtime=json.dumps(policy.runtime),
        rejected=rej,
        failures="\n\n".join(_failure_digest(r) for r in failures),
    )
    out = llm.chat(llm.cfg.improver_model, [{"role": "user", "content": prompt}], json_mode=True,
                   temperature=0.2, cache_salt=f"improve-v{policy.version}-{len(rejected)}")
    proposal = parse_json(out["content"])
    proposal.setdefault("changes", [])
    valid, dropped = [], []
    for ch in proposal["changes"][:max_changes]:
        if ch.get("op") == "set_tool_note" and ch.get("target") not in tool_names:
            dropped.append({**ch, "dropped_because": "unknown tool"})
            continue
        why = validate_change(ch, scenario_ids)
        (dropped.append({**ch, "dropped_because": why}) if why else valid.append(ch))
    proposal["changes"] = valid
    proposal["dropped_changes"] = dropped
    return proposal
