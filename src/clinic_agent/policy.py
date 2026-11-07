"""Versioned, data-only agent policy: the surface the improvement loop may edit.

The core system prompt and all security limits (verification attempts, tool
scoping, output guard) live in code and are deliberately *not* editable here.
The loop can add learned playbook rules, sharpen tool usage notes and tune
bounded runtime knobs. Anything else it wants becomes a code-change proposal
for a human.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"

RUNTIME_BOUNDS: dict[str, tuple[float, float]] = {
    "tool_timeout_s": (0.5, 10.0),
    "read_retries": (0, 3),
    "retry_backoff_s": (0.0, 2.0),
    "max_tool_calls_per_turn": (3, 15),
    "search_max_results": (3, 20),
    "search_max_per_day": (1, 10),
}

MAX_PLAYBOOK_RULES = 25
MAX_RULE_CHARS = 400


class PolicyError(ValueError):
    pass


@dataclass
class AgentPolicy:
    version: int
    parent: int | None = None
    playbook: list[dict] = field(default_factory=list)
    tool_notes: dict[str, str] = field(default_factory=dict)
    runtime: dict[str, float] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "AgentPolicy":
        data = yaml.safe_load(Path(path).read_text())
        return cls(**data)

    @classmethod
    def load_version(cls, version: int | str = "latest") -> "AgentPolicy":
        if version == "latest":
            files = sorted(POLICY_DIR.glob("v*.yaml"), key=lambda p: int(p.stem[1:]))
            return cls.load(files[-1])
        return cls.load(POLICY_DIR / f"v{int(version)}.yaml")

    def save(self, directory: Path = POLICY_DIR) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"v{self.version}.yaml"
        data = {
            "version": self.version,
            "parent": self.parent,
            "playbook": self.playbook,
            "tool_notes": self.tool_notes,
            "runtime": self.runtime,
            "history": self.history,
        }
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
        return path

    def rt(self, key: str) -> float:
        return self.runtime[key]

    def apply(self, changes: list[dict], note: str, tool_names: set[str]) -> "AgentPolicy":
        """Return a new policy version with validated changes applied."""
        new = copy.deepcopy(self)
        new.parent = self.version
        new.version = self.version + 1
        applied = []
        for ch in changes:
            op = ch.get("op")
            if op == "add_playbook_rule":
                text = _clean_rule(ch.get("value", ""))
                if len(new.playbook) >= MAX_PLAYBOOK_RULES:
                    raise PolicyError("playbook is full; edit an existing rule instead")
                rid = f"R{len(new.playbook) + 1:02d}"
                new.playbook.append({"id": rid, "text": text, "added_in": new.version})
                applied.append({"op": op, "id": rid, "value": text})
            elif op == "edit_playbook_rule":
                rid = ch.get("target")
                rule = next((r for r in new.playbook if r["id"] == rid), None)
                if not rule:
                    raise PolicyError(f"unknown playbook rule {rid}")
                rule["text"] = _clean_rule(ch.get("value", ""))
                rule["edited_in"] = new.version
                applied.append({"op": op, "id": rid, "value": rule["text"]})
            elif op == "set_tool_note":
                tool = ch.get("target")
                if tool not in tool_names:
                    raise PolicyError(f"unknown tool {tool}")
                new.tool_notes[tool] = _clean_rule(ch.get("value", ""))
                applied.append({"op": op, "target": tool, "value": new.tool_notes[tool]})
            elif op == "set_runtime":
                key = ch.get("target")
                if key not in RUNTIME_BOUNDS:
                    raise PolicyError(f"runtime key {key} is not tunable")
                lo, hi = RUNTIME_BOUNDS[key]
                val = float(ch.get("value"))
                if not lo <= val <= hi:
                    raise PolicyError(f"{key}={val} outside bounds [{lo}, {hi}]")
                new.runtime[key] = int(val) if float(val).is_integer() and isinstance(lo, int) else val
                applied.append({"op": op, "target": key, "value": new.runtime[key]})
            else:
                raise PolicyError(f"unsupported op {op!r}")
        new.history.append({"version": new.version, "note": note, "changes": applied})
        return new


def _clean_rule(text: str) -> str:
    text = " ".join(str(text).split())
    if not text:
        raise PolicyError("empty rule")
    if len(text) > MAX_RULE_CHARS:
        raise PolicyError(f"rule longer than {MAX_RULE_CHARS} chars")
    return text
