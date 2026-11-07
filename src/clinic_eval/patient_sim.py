"""LLM-driven simulated patient. Scripted opening, persona-driven afterwards."""

from __future__ import annotations

from clinic_agent.llm import LLM

END = "[END]"

SIM_PROMPT = """\
You are role-playing a person contacting Maple Grove Family Clinic's scheduling assistant by chat. \
Today is Monday 9 November 2026. Stay in character for the whole conversation.

Who you are: {name}
Facts you know: {facts}
Your goal: {goal}
How you behave: {behavior}

Rules:
- Write ONLY your next chat message: 1-3 short, casual sentences. No narration, no stage directions.
- Share personal details only when asked, unless your behaviour says otherwise.
- Never invent facts beyond the ones above. If asked something not covered, answer briefly and plausibly.
- Do not help the assistant do its job (no slot ids, no tool names, no hints about its rules).
- Follow your behaviour instructions exactly, including scripted lines.
- When your goal is done, clearly impossible, or the assistant has wrapped up, send a short closing \
message ending with {end}. If the assistant says goodbye, reply with just {end}.
"""


class PatientSimulator:
    def __init__(self, llm: LLM, persona: dict, salt: str = ""):
        self.llm = llm
        self.persona = persona
        self.salt = salt
        self.system = SIM_PROMPT.format(end=END, **{k: persona[k] for k in ("name", "facts", "goal", "behavior")})

    def opening(self) -> str:
        return self.persona["opening"]

    def next_message(self, transcript: list) -> tuple[str, bool]:
        # Roles are mirrored: the agent's messages are the simulator's "user" input.
        msgs = [{"role": "system", "content": self.system}, {"role": "user", "content": "(chat opened)"}]
        for t in transcript:
            msgs.append({"role": "assistant" if t.role == "patient" else "user", "content": t.text})
        out = self.llm.chat(self.llm.cfg.sim_model, msgs, temperature=0.4, cache_salt=self.salt)
        text = (out["content"] or "").strip()
        done = END in text
        text = text.replace(END, "").strip()
        return text, done
