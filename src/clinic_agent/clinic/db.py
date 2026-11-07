"""In-memory clinic store. Business invariants live here, not in the prompt.

The LLM is never trusted to enforce a rule that can be enforced in code:
double-booking, overlapping appointments, ownership checks and the late
cancellation window are all checked by the store itself.
"""

from __future__ import annotations

import copy
import unicodedata
from datetime import date, datetime, timedelta

from .data import DEFAULT_NOW, generate_world

LATE_CANCEL_HOURS = 24
LATE_CANCEL_FEE = 50


class ClinicError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def normalize_name(name: str) -> str:
    stripped = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(stripped.lower().replace(".", " ").split())


def part_of_day(t: datetime) -> str:
    return "morning" if t.hour < 12 else "afternoon"


class Clinic:
    def __init__(self, now: datetime = DEFAULT_NOW, world: dict | None = None):
        self.world = copy.deepcopy(world) if world else generate_world(now)
        self.now: datetime = self.world["now"]
        self._idempotency: dict[str, str] = {}
        self._next_appt = 5000
        self._next_patient = 100

    # ---- read helpers -------------------------------------------------
    @property
    def providers(self) -> dict:
        return self.world["providers"]

    @property
    def patients(self) -> dict:
        return self.world["patients"]

    @property
    def slots(self) -> dict:
        return self.world["slots"]

    @property
    def appointments(self) -> dict:
        return self.world["appointments"]

    def find_patients(self, full_name: str, dob: str) -> list[dict]:
        n = normalize_name(full_name)
        return [p for p in self.patients.values() if normalize_name(p["name"]) == n and p["dob"] == dob]

    def describe_slot(self, sid: str) -> dict:
        s = self.slots[sid]
        p = self.providers[s["provider_id"]]
        return {
            "slot_id": sid,
            "provider_id": p["id"],
            "provider": p["name"],
            "specialty": p["specialty"],
            "date": s["start"].strftime("%Y-%m-%d"),
            "weekday": s["start"].strftime("%A"),
            "time": s["start"].strftime("%H:%M"),
        }

    def describe_appointment(self, appt_id: str) -> dict:
        a = self.appointments[appt_id]
        return {"appointment_id": a["id"], "status": a["status"], "reason": a["reason"], **self.describe_slot(a["slot_id"])}

    # ---- queries ------------------------------------------------------
    def search_slots(
        self,
        date_from: date,
        date_to: date,
        provider_id: str | None = None,
        specialty: str | None = None,
        when: str = "any",
    ) -> list[dict]:
        out = []
        for sid in sorted(self.slots, key=lambda k: (self.slots[k]["start"], k)):
            s = self.slots[sid]
            if s["appointment_id"] or s["start"] <= self.now:
                continue
            if not (date_from <= s["start"].date() <= date_to):
                continue
            prov = self.providers[s["provider_id"]]
            if provider_id and prov["id"] != provider_id:
                continue
            if specialty and prov["specialty"] != specialty:
                continue
            if when in ("morning", "afternoon") and part_of_day(s["start"]) != when:
                continue
            out.append(self.describe_slot(sid))
        return out

    def patient_appointments(self, patient_id: str, include_past: bool = False) -> list[dict]:
        appts = [
            a for a in self.appointments.values()
            if a["patient_id"] == patient_id and a["status"] == "booked" and (include_past or a["start"] >= self.now)
        ]
        return [self.describe_appointment(a["id"]) for a in sorted(appts, key=lambda a: a["start"])]

    # ---- writes -------------------------------------------------------
    def register_patient(self, full_name: str, dob: str, phone: str) -> dict:
        if self.find_patients(full_name, dob):
            raise ClinicError("ALREADY_EXISTS", "A patient with this name and date of birth already exists.")
        self._next_patient += 1
        pid = f"P{self._next_patient}"
        self.patients[pid] = {"id": pid, "name": full_name.strip(), "dob": dob, "phone": phone, "new": True}
        return self.patients[pid]

    def book(self, patient_id: str, sid: str, reason: str, idempotency_key: str | None = None) -> dict:
        if idempotency_key and idempotency_key in self._idempotency:
            return self.describe_appointment(self._idempotency[idempotency_key])
        if sid not in self.slots:
            raise ClinicError("UNKNOWN_SLOT", f"Slot {sid} does not exist. Use search_availability to get valid slot ids.")
        s = self.slots[sid]
        if s["start"] <= self.now:
            raise ClinicError("SLOT_IN_PAST", "That slot is in the past.")
        if s["appointment_id"]:
            raise ClinicError("SLOT_TAKEN", "That slot was just taken by someone else. Search again and offer alternatives.")
        self._check_age_fit(patient_id, s["provider_id"])
        for a in self.appointments.values():
            if a["patient_id"] == patient_id and a["status"] == "booked":
                other = self.slots[a["slot_id"]]
                if other["start"] < s["end"] and s["start"] < other["end"]:
                    raise ClinicError(
                        "PATIENT_OVERLAP",
                        "Patient already has an appointment at that time.",
                        conflicting_appointment=self.describe_appointment(a["id"]),
                    )
        self._next_appt += 1
        aid = f"A{self._next_appt}"
        self.appointments[aid] = {
            "id": aid,
            "patient_id": patient_id,
            "slot_id": sid,
            "provider_id": s["provider_id"],
            "start": s["start"],
            "reason": reason,
            "status": "booked",
            "created_at": self.now,
        }
        s["appointment_id"] = aid
        if idempotency_key:
            self._idempotency[idempotency_key] = aid
        return self.describe_appointment(aid)

    def _check_age_fit(self, patient_id: str, provider_id: str) -> None:
        dob = datetime.strptime(self.patients[patient_id]["dob"], "%Y-%m-%d").date()
        age = (self.now.date() - dob).days // 365
        if self.providers[provider_id]["specialty"] == "pediatrics" and age >= 18:
            raise ClinicError("AGE_MISMATCH", "Pediatrics only sees patients under 18.")

    def _owned(self, patient_id: str, appt_id: str) -> dict:
        a = self.appointments.get(appt_id)
        # Same error for "doesn't exist" and "not yours": never confirm another patient's appointment exists.
        if not a or a["patient_id"] != patient_id or a["status"] != "booked":
            raise ClinicError("NOT_FOUND", "No active appointment with that id for the verified patient.")
        return a

    def is_late_cancel(self, appt_id: str) -> bool:
        a = self.appointments[appt_id]
        return a["start"] - self.now < timedelta(hours=LATE_CANCEL_HOURS)

    def cancel(self, patient_id: str, appt_id: str) -> dict:
        a = self._owned(patient_id, appt_id)
        late = self.is_late_cancel(appt_id)
        a["status"] = "cancelled"
        a["late_fee"] = LATE_CANCEL_FEE if late else 0
        self.slots[a["slot_id"]]["appointment_id"] = None
        return {**self.describe_appointment(appt_id), "late_cancellation": late, "fee_usd": a["late_fee"]}

    def reschedule(self, patient_id: str, appt_id: str, new_sid: str) -> dict:
        a = self._owned(patient_id, appt_id)
        late = self.is_late_cancel(appt_id)
        old_sid = a["slot_id"]
        # Free the old slot first so moving within an overlapping window works; restore on failure (atomic).
        a["status"] = "cancelled"
        self.slots[old_sid]["appointment_id"] = None
        try:
            new = self.book(patient_id, new_sid, a["reason"])
        except ClinicError:
            a["status"] = "booked"
            self.slots[old_sid]["appointment_id"] = appt_id
            raise
        a["status"] = "rescheduled"
        a["rescheduled_to"] = new["appointment_id"]
        a["late_fee"] = LATE_CANCEL_FEE if late else 0
        return {"old_appointment_id": appt_id, "new_appointment": new, "late_change": late, "fee_usd": a["late_fee"]}
