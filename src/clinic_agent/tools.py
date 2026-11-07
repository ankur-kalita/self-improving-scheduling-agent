"""Tools exposed to the agent, scoped to a session.

Design rules:
* The model never passes a patient id. Identity is bound to the session by
  `verify_patient`, so a prompt-injected model still cannot read or modify
  another patient's data.
* Every result is structured `{"ok": bool, ...}` with a machine-readable error
  code, so failures are explicit instead of being narrated away.
* Retries happen here, not in the model: reads are retried on transient
  failure; writes carry an idempotency key so a retry can never double-book.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .clinic.data import HORIZON_DAYS
from .clinic.db import Clinic, ClinicError
from .clinic.kb import search_kb
from .policy import AgentPolicy

MAX_VERIFY_ATTEMPTS = 3
SPECIALTIES = ["family_medicine", "pediatrics", "dermatology"]


class TransientToolError(Exception):
    pass


@dataclass
class SessionState:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    active_patient_id: str | None = None
    verified_patient_ids: list[str] = field(default_factory=list)
    verify_failures: int = 0
    locked: bool = False
    escalations: list[dict] = field(default_factory=list)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., dict]
    retry_safe: bool = False
    requires_verified: bool = False


@dataclass
class ToolEvent:
    name: str
    args: dict
    result: dict
    attempts: int
    latency_s: float
    turn: int


class ToolBox:
    def __init__(self, clinic: Clinic, policy: AgentPolicy, faults: list[dict] | None = None):
        self.clinic = clinic
        self.policy = policy
        self.session = SessionState()
        self.events: list[ToolEvent] = []
        self.faults = [dict(f, remaining=f.get("times", 1)) for f in (faults or [])]
        self.turn = 0
        self.specs = {s.name: s for s in self._build_specs()}

    # ---- schema -------------------------------------------------------
    def openai_tools(self) -> list[dict]:
        out = []
        for s in self.specs.values():
            desc = s.description
            note = self.policy.tool_notes.get(s.name)
            if note:
                desc += f"\nUsage note: {note}"
            out.append({"type": "function", "function": {"name": s.name, "description": desc, "parameters": s.parameters}})
        return out

    def _build_specs(self) -> list[ToolSpec]:
        obj = lambda props, req: {"type": "object", "properties": props, "required": req}  # noqa: E731
        return [
            ToolSpec(
                "verify_patient",
                "Verify the caller's identity with full name AND date of birth. Must succeed before any personal "
                "information is shared or any appointment is created, changed or cancelled. Calling it again with "
                "another person's details (e.g. a parent acting for a child) switches the active patient.",
                obj({
                    "full_name": {"type": "string"},
                    "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
                }, ["full_name", "date_of_birth"]),
                self._verify_patient,
                retry_safe=True,
            ),
            ToolSpec(
                "register_patient",
                "Register a NEW patient. Only use after verify_patient failed because the person has never visited, "
                "and the caller confirmed they are new. Sets them as the active patient.",
                obj({
                    "full_name": {"type": "string"},
                    "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
                    "phone": {"type": "string"},
                }, ["full_name", "date_of_birth", "phone"]),
                self._register_patient,
            ),
            ToolSpec(
                "list_providers",
                "List the clinic's providers, their specialty and working days.",
                obj({"specialty": {"type": "string", "enum": SPECIALTIES}}, []),
                self._list_providers,
                retry_safe=True,
            ),
            ToolSpec(
                "search_availability",
                "Search open appointment slots. Only offer slots returned by this tool; never invent times. "
                "Provide specialty or provider_id. Dates are YYYY-MM-DD and inclusive.",
                obj({
                    "specialty": {"type": "string", "enum": SPECIALTIES},
                    "provider_id": {"type": "string"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                    "part_of_day": {"type": "string", "enum": ["any", "morning", "afternoon"]},
                }, ["date_from", "date_to"]),
                self._search_availability,
                retry_safe=True,
            ),
            ToolSpec(
                "book_appointment",
                "Book a slot for the active verified patient. Only call after the patient explicitly confirmed "
                "the exact provider, date and time.",
                obj({
                    "slot_id": {"type": "string"},
                    "reason": {"type": "string", "description": "Short non-diagnostic reason for visit"},
                }, ["slot_id", "reason"]),
                self._book,
                retry_safe=True,  # idempotency key makes retries safe
                requires_verified=True,
            ),
            ToolSpec(
                "get_my_appointments",
                "List upcoming appointments of the active verified patient.",
                obj({}, []),
                self._my_appointments,
                retry_safe=True,
                requires_verified=True,
            ),
            ToolSpec(
                "cancel_appointment",
                "Cancel one of the active verified patient's appointments. Only after explicit confirmation.",
                obj({"appointment_id": {"type": "string"}}, ["appointment_id"]),
                self._cancel,
                requires_verified=True,
            ),
            ToolSpec(
                "reschedule_appointment",
                "Atomically move one of the active patient's appointments to a new slot (old one is released only "
                "if the new booking succeeds). Only after explicit confirmation.",
                obj({"appointment_id": {"type": "string"}, "new_slot_id": {"type": "string"}},
                    ["appointment_id", "new_slot_id"]),
                self._reschedule,
                requires_verified=True,
            ),
            ToolSpec(
                "get_clinic_info",
                "Look up clinic facts: hours, address, parking, cancellation fees, insurance, new-patient info, "
                "prescriptions, nurse line, billing. Use this instead of answering policy questions from memory.",
                obj({"question": {"type": "string"}}, ["question"]),
                self._clinic_info,
                retry_safe=True,
            ),
            ToolSpec(
                "escalate_to_human",
                "Hand off to clinic staff. Use urgency 'emergency' for possible medical emergencies (after telling "
                "the patient to call 911), 'urgent' for same-day clinical concerns, 'routine' for anything you "
                "cannot or should not handle (billing disputes, clinical questions, repeated tool failures).",
                obj({
                    "reason": {"type": "string"},
                    "urgency": {"type": "string", "enum": ["routine", "urgent", "emergency"]},
                    "summary": {"type": "string", "description": "What staff need to know; no diagnosis"},
                }, ["reason", "urgency", "summary"]),
                self._escalate,
                retry_safe=True,
            ),
        ]

    # ---- execution ----------------------------------------------------
    def execute(self, name: str, raw_args: str | dict) -> dict:
        t0 = time.monotonic()
        attempts = 0
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        except json.JSONDecodeError:
            args = {}
            result = _err("BAD_ARGUMENTS", "Arguments were not valid JSON.")
            self._record(name, args, result, 0, t0)
            return result

        spec = self.specs.get(name)
        if not spec:
            result = _err("UNKNOWN_TOOL", f"No tool named {name}.")
        elif spec.requires_verified and not self.session.active_patient_id:
            result = _err("NOT_VERIFIED", "Verify the patient's identity with verify_patient first.")
        elif self.session.locked and name in ("verify_patient", "register_patient"):
            result = _err("LOCKED", "Too many failed verification attempts. Offer a transfer to clinic staff.")
        else:
            max_attempts = 1 + (int(self.policy.rt("read_retries")) if spec.retry_safe else 0)
            result = None
            while attempts < max_attempts:
                attempts += 1
                try:
                    result = self._call_with_faults(spec, args)
                    break
                except TransientToolError as e:
                    result = _err(str(e), "The scheduling system did not respond.", retryable=True)
                    if attempts < max_attempts:
                        time.sleep(self.policy.rt("retry_backoff_s") * attempts)
                except ClinicError as e:
                    result = _err(e.code, e.message, **e.extra)
                    break
                except (TypeError, ValueError) as e:
                    result = _err("BAD_ARGUMENTS", str(e))
                    break
            if result.get("retryable"):
                result["attempts"] = attempts
        self._record(name, args, result, attempts, t0)
        return result

    def _record(self, name, args, result, attempts, t0):
        self.events.append(ToolEvent(name, args, result, attempts, round(time.monotonic() - t0, 3), self.turn))

    def _call_with_faults(self, spec: ToolSpec, args: dict) -> dict:
        for f in self.faults:
            if f["tool"] != spec.name or f["remaining"] <= 0:
                continue
            f["remaining"] -= 1
            if f["kind"] == "timeout":
                # Simulated hung backend: we wait out the configured timeout, then give up.
                time.sleep(min(self.policy.rt("tool_timeout_s"), f.get("hang_s", 30)))
                raise TransientToolError("TIMEOUT")
            if f["kind"] == "unavailable":
                raise TransientToolError("SERVICE_UNAVAILABLE")
            if f["kind"] == "race" and spec.name == "book_appointment":
                self._steal_slot(args.get("slot_id", ""))
        return {"ok": True, **spec.handler(**args)}

    def _steal_slot(self, sid: str) -> None:
        s = self.clinic.slots.get(sid)
        if s and not s["appointment_id"]:
            s["appointment_id"] = "A-RACE"
            self.clinic.appointments["A-RACE"] = {
                "id": "A-RACE", "patient_id": "P009", "slot_id": sid, "provider_id": s["provider_id"],
                "start": s["start"], "reason": "routine visit", "status": "booked", "created_at": self.clinic.now,
            }

    # ---- handlers -----------------------------------------------------
    def _verify_patient(self, full_name: str, date_of_birth: str) -> dict:
        dob = _parse_date(date_of_birth).isoformat()
        matches = self.clinic.find_patients(full_name, dob)
        if len(matches) != 1:
            self.session.verify_failures += 1
            if self.session.verify_failures >= MAX_VERIFY_ATTEMPTS:
                self.session.locked = True
            # Deliberately identical for "no such name" and "wrong DOB": no enumeration oracle.
            raise ClinicError(
                "NOT_VERIFIED",
                "No patient matches that name and date of birth. Do not reveal whether the name exists. "
                "Ask the caller to double-check, or offer new-patient registration.",
                attempts_left=max(0, MAX_VERIFY_ATTEMPTS - self.session.verify_failures),
            )
        p = matches[0]
        self._activate(p["id"])
        return {"patient_id": p["id"], "first_name": p["name"].split()[0], "is_minor": self._is_minor(p)}

    def _register_patient(self, full_name: str, date_of_birth: str, phone: str) -> dict:
        dob = _parse_date(date_of_birth)
        if dob >= self.clinic.now.date():
            raise ValueError("date_of_birth must be in the past")
        p = self.clinic.register_patient(full_name, dob.isoformat(), phone)
        self._activate(p["id"])
        return {"patient_id": p["id"], "first_name": p["name"].split()[0], "is_minor": self._is_minor(p)}

    def _activate(self, pid: str) -> None:
        self.session.active_patient_id = pid
        if pid not in self.session.verified_patient_ids:
            self.session.verified_patient_ids.append(pid)

    def _is_minor(self, p: dict) -> bool:
        dob = datetime.strptime(p["dob"], "%Y-%m-%d").date()
        return (self.clinic.now.date() - dob).days // 365 < 18

    def _list_providers(self, specialty: str | None = None) -> dict:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        provs = [
            {"provider_id": p["id"], "name": p["name"], "specialty": p["specialty"],
             "working_days": [days[d] for d in p["days"]]}
            for p in self.clinic.providers.values() if not specialty or p["specialty"] == specialty
        ]
        return {"providers": provs}

    def _search_availability(self, date_from: str, date_to: str, specialty: str | None = None,
                             provider_id: str | None = None, part_of_day: str = "any") -> dict:
        if not specialty and not provider_id:
            raise ValueError("provide specialty or provider_id")
        d0, d1 = _parse_date(date_from), _parse_date(date_to)
        today = self.clinic.now.date()
        horizon = today + timedelta(days=HORIZON_DAYS - 1)
        if d1 < d0:
            raise ValueError("date_to is before date_from")
        d0, d1 = max(d0, today), min(d1, horizon)
        hits = self.clinic.search_slots(d0, d1, provider_id, specialty, part_of_day or "any")
        per_day: dict[str, int] = {}
        shown = []
        for h in hits:
            key = h["date"] + h["provider_id"]
            if per_day.get(key, 0) >= self.policy.rt("search_max_per_day"):
                continue
            per_day[key] = per_day.get(key, 0) + 1
            shown.append(h)
            if len(shown) >= self.policy.rt("search_max_results"):
                break
        out = {"searched": {"from": d0.isoformat(), "to": d1.isoformat()}, "slots": shown,
               "total_matching": len(hits)}
        if not hits:
            out["note"] = f"No open slots in range. Bookable horizon ends {horizon.isoformat()}."
        return out

    def _book(self, slot_id: str, reason: str) -> dict:
        pid = self.session.active_patient_id
        key = f"{self.session.session_id}:{pid}:{slot_id}"
        return {"appointment": self.clinic.book(pid, slot_id, reason, idempotency_key=key)}

    def _my_appointments(self) -> dict:
        return {"appointments": self.clinic.patient_appointments(self.session.active_patient_id)}

    def _cancel(self, appointment_id: str) -> dict:
        return self.clinic.cancel(self.session.active_patient_id, appointment_id)

    def _reschedule(self, appointment_id: str, new_slot_id: str) -> dict:
        return self.clinic.reschedule(self.session.active_patient_id, appointment_id, new_slot_id)

    def _clinic_info(self, question: str) -> dict:
        hits = search_kb(question)
        return {"passages": hits} if hits else {"passages": [], "note": "Nothing found; do not guess, offer staff."}

    def _escalate(self, reason: str, urgency: str, summary: str) -> dict:
        ticket = f"T{len(self.session.escalations) + 1:03d}"
        self.session.escalations.append({"ticket": ticket, "reason": reason, "urgency": urgency, "summary": summary})
        msg = {
            "emergency": "Staff alerted. The patient must call 911 now; do not continue scheduling.",
            "urgent": "A nurse will call the patient back within 1 hour.",
            "routine": "A staff member will call the patient back within 1 business day.",
        }[urgency]
        return {"ticket": ticket, "message": msg}


def _err(code: str, message: str, **extra: Any) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"invalid date {s!r}; use YYYY-MM-DD") from None
