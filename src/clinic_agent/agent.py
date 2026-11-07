"""The scheduling agent: a tool-calling loop over explicit session state."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .clinic.db import Clinic
from .llm import LLM
from .policy import AgentPolicy
from .prompts import render_system_prompt
from .tools import ToolBox

FALLBACK_MESSAGE = (
    "I'm sorry, I'm having trouble completing that right now. I've asked a member of our staff to call you back."
)
GUARD_MESSAGE = "I'm sorry, I can only share information about the verified patient on this call."


@dataclass
class Turn:
    role: str  # "patient" | "agent"
    text: str
    guard: str | None = None


@dataclass
class Agent:
    clinic: Clinic
    policy: AgentPolicy
    llm: LLM
    model: str
    faults: list[dict] | None = None
    cache_salt: str = ""
    tools: ToolBox = field(init=False)
    messages: list[dict] = field(default_factory=list)
    transcript: list[Turn] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.tools = ToolBox(self.clinic, self.policy, self.faults)

    # The session line is re-rendered every step, so the model always sees the
    # *current* verified identity rather than inferring it from old messages.
    def _session_line(self) -> str:
        s = self.tools.session
        if not s.active_patient_id:
            line = "No patient verified yet."
        else:
            p = self.clinic.patients[s.active_patient_id]
            line = f"Active verified patient: {p['name']} (id {p['id']})."
            others = [self.clinic.patients[i]["name"] for i in s.verified_patient_ids if i != s.active_patient_id]
            if others:
                line += f" Also verified earlier in this call: {', '.join(others)}."
        if s.locked:
            line += " Verification is LOCKED after too many failures: offer a transfer to staff."
        if s.escalations:
            line += f" Escalations raised: {', '.join(e['urgency'] for e in s.escalations)}."
        return line

    def respond(self, user_text: str) -> str:
        self.tools.turn += 1
        self.transcript.append(Turn("patient", user_text))
        self.messages.append({"role": "user", "content": user_text})
        max_calls = int(self.policy.rt("max_tool_calls_per_turn"))
        calls = 0
        while True:
            system = render_system_prompt(self.clinic.now, self._session_line(), self.policy.playbook)
            try:
                out = self.llm.chat(
                    self.model,
                    [{"role": "system", "content": system}, *self.messages],
                    tools=self.tools.openai_tools(),
                    cache_salt=self.cache_salt,
                )
            except Exception as e:  # provider failure: degrade safely instead of crashing the call
                self.errors.append(f"llm_error: {type(e).__name__}: {e}")
                return self._fallback("llm_error")

            if out["tool_calls"] and calls < max_calls:
                self.messages.append({
                    "role": "assistant",
                    "content": out["content"] or None,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]},
                         **({"extra_content": tc["extra_content"]} if tc.get("extra_content") else {})}
                        for tc in out["tool_calls"]
                    ],
                })
                for tc in out["tool_calls"]:
                    calls += 1
                    result = self.tools.execute(tc["name"], tc["arguments"])
                    self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, default=str)})
                continue
            if out["tool_calls"]:
                self.errors.append("tool_budget_exceeded")
                return self._fallback("tool_budget_exceeded")

            text = (out["content"] or "").strip()
            if not text:
                self.errors.append("empty_reply")
                text = "Sorry, could you say that again?"
            guard = self._privacy_guard(text)
            if guard:
                self.errors.append(f"privacy_guard: {guard}")
                text = GUARD_MESSAGE
            self.messages.append({"role": "assistant", "content": text})
            self.transcript.append(Turn("agent", text, guard))
            return text

    def _fallback(self, reason: str) -> str:
        self.tools.execute("escalate_to_human", {"reason": reason, "urgency": "routine",
                                                 "summary": "Assistant failed mid-conversation; please call back."})
        self.messages.append({"role": "assistant", "content": FALLBACK_MESSAGE})
        self.transcript.append(Turn("agent", FALLBACK_MESSAGE))
        return FALLBACK_MESSAGE

    def _privacy_guard(self, text: str) -> str | None:
        """Deterministic last line of defence: block other patients' names/DOBs the caller didn't say."""
        said_by_user = " ".join(t.text for t in self.transcript if t.role == "patient").lower()
        verified = set(self.tools.session.verified_patient_ids)
        low = text.lower()
        for p in self.clinic.patients.values():
            if p["id"] in verified:
                continue
            if p["dob"] in text:
                return f"dob of {p['id']}"
            name = p["name"].lower()
            if re.search(rf"\b{re.escape(name)}\b", low) and name not in said_by_user:
                return f"name of {p['id']}"
        return None
