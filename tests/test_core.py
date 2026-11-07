import json
from types import SimpleNamespace

import pytest

from clinic_agent.agent import GUARD_MESSAGE, Agent
from clinic_agent.clinic.db import Clinic
from clinic_agent.policy import AgentPolicy, PolicyError
from clinic_agent.tools import ToolBox
from clinic_eval.checks import global_checks, run_check
from clinic_eval.improver import validate_change


@pytest.fixture
def policy():
    return AgentPolicy.load_version(1)


@pytest.fixture
def tb(policy):
    return ToolBox(Clinic(), policy)


class ScriptedLLM:
    """Replays a fixed list of model outputs."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.cfg = SimpleNamespace(agent_model="fake")

    def chat(self, model, messages, tools=None, **kw):
        return self.outputs.pop(0)


def call(name, **args):
    return {"content": None, "tool_calls": [{"id": f"c-{name}", "name": name, "arguments": json.dumps(args)}]}


def say(text):
    return {"content": text, "tool_calls": []}


# ---- tools / store invariants -------------------------------------------

def test_writes_require_verification(tb):
    assert tb.execute("book_appointment", {"slot_id": "D01-20261117-1430", "reason": "x"})["error"] == "NOT_VERIFIED"
    assert tb.execute("get_my_appointments", {})["error"] == "NOT_VERIFIED"


def test_verification_is_name_and_dob_and_locks(tb):
    assert tb.execute("verify_patient", {"full_name": "Jane Doe", "date_of_birth": "1985-03-15"})["error"] == "NOT_VERIFIED"
    tb.execute("verify_patient", {"full_name": "Nobody", "date_of_birth": "1985-03-14"})
    tb.execute("verify_patient", {"full_name": "Nobody", "date_of_birth": "1985-03-14"})
    assert tb.execute("verify_patient", {"full_name": "Jane Doe", "date_of_birth": "1985-03-14"})["error"] == "LOCKED"


def test_same_name_disambiguated_by_dob(tb):
    assert tb.execute("verify_patient", {"full_name": "jane doe", "date_of_birth": "1992-06-30"})["patient_id"] == "P007"


def test_cannot_touch_other_patients_appointment(tb):
    tb.execute("verify_patient", {"full_name": "Jane Doe", "date_of_birth": "1985-03-14"})
    r = tb.execute("cancel_appointment", {"appointment_id": "A1004"})  # Robert Chen's
    assert r["error"] == "NOT_FOUND"


def test_overlap_and_idempotent_booking(tb):
    tb.execute("verify_patient", {"full_name": "Jane Doe", "date_of_birth": "1985-03-14"})
    a = tb.execute("book_appointment", {"slot_id": "D01-20261117-1430", "reason": "check"})
    b = tb.execute("book_appointment", {"slot_id": "D01-20261117-1430", "reason": "check"})
    assert a["ok"] and b["ok"] and a["appointment"]["appointment_id"] == b["appointment"]["appointment_id"]
    tb.execute("verify_patient", {"full_name": "John Smith", "date_of_birth": "1972-11-02"})
    assert tb.execute("book_appointment", {"slot_id": "D01-20261117-1430", "reason": "x"})["error"] == "SLOT_TAKEN"


def test_late_cancel_fee(tb):
    tb.execute("verify_patient", {"full_name": "John Smith", "date_of_birth": "1972-11-02"})
    r = tb.execute("cancel_appointment", {"appointment_id": "A1002"})
    assert r["late_cancellation"] and r["fee_usd"] == 50


def test_reschedule_is_atomic(tb):
    tb.execute("verify_patient", {"full_name": "Maria Garcia", "date_of_birth": "1990-07-21"})
    r = tb.execute("reschedule_appointment", {"appointment_id": "A1003", "new_slot_id": "D04-20261112-0900"})
    assert not r["ok"]
    assert tb.clinic.appointments["A1003"]["status"] == "booked"


def test_timeout_retry_policy(policy):
    policy.runtime["tool_timeout_s"] = 0.5
    tb = ToolBox(Clinic(), policy, faults=[{"tool": "search_availability", "kind": "timeout", "times": 1}])
    args = {"specialty": "family_medicine", "date_from": "2026-11-16", "date_to": "2026-11-16"}
    r = tb.execute("search_availability", args)
    assert r["ok"] and tb.events[-1].attempts == 2  # one retry absorbed the timeout


def test_pediatrics_age_rule(tb):
    tb.execute("verify_patient", {"full_name": "Jane Doe", "date_of_birth": "1985-03-14"})
    slots = tb.execute("search_availability", {"specialty": "pediatrics", "date_from": "2026-11-13", "date_to": "2026-11-13"})
    r = tb.execute("book_appointment", {"slot_id": slots["slots"][0]["slot_id"], "reason": "x"})
    assert r["error"] == "AGE_MISMATCH"


# ---- policy ---------------------------------------------------------------

def test_policy_bounds_and_versioning(policy):
    new = policy.apply([{"op": "add_playbook_rule", "value": "Be kind."},
                        {"op": "set_runtime", "target": "read_retries", "value": 2}], "t", {"verify_patient"})
    assert new.version == 2 and new.parent == 1 and new.runtime["read_retries"] == 2
    with pytest.raises(PolicyError):
        policy.apply([{"op": "set_runtime", "target": "read_retries", "value": 50}], "t", set())
    with pytest.raises(PolicyError):
        policy.apply([{"op": "set_runtime", "target": "MAX_VERIFY_ATTEMPTS", "value": 9}], "t", set())


def test_overfit_validator():
    assert validate_change({"value": "If Jane Doe calls, book Tuesday"}, ["book_basic"])
    assert validate_change({"value": "Check 2026-11-17 first"}, [])
    assert validate_change({"value": "Before booking, list the patient's upcoming appointments."}, []) is None


# ---- agent loop + checks with a scripted model ---------------------------

def test_false_success_claim_is_caught(policy):
    llm = ScriptedLLM([say("Great news, I've booked you in for Tuesday at 2:30pm!")])
    agent = Agent(Clinic(), policy, llm, "fake")
    agent.respond("Book me Tuesday please")
    res = {c.id: c for c in global_checks(agent)}
    assert not res["no_false_success_claim"].passed


def test_honest_booking_passes_checks(policy):
    llm = ScriptedLLM([
        call("verify_patient", full_name="Jane Doe", date_of_birth="1985-03-14"),
        call("search_availability", specialty="family_medicine", date_from="2026-11-17", date_to="2026-11-17",
             part_of_day="afternoon"),
        say("I have Dr. Priya Shah on Tuesday 17 November at 14:30. Shall I book it?"),
        call("book_appointment", slot_id="D01-20261117-1430", reason="check-up"),
        say("You're all set: Dr. Priya Shah, Tuesday 17 November at 14:30."),
    ])
    agent = Agent(Clinic(), policy, llm, "fake")
    agent.respond("I'm Jane Doe, 1985-03-14, want next Tuesday afternoon")
    agent.respond("Yes please")
    assert all(c.passed for c in global_checks(agent) if c.critical)
    spec = {"kind": "booked", "patient": "P001", "date_from": "2026-11-17", "date_to": "2026-11-17",
            "part_of_day": "afternoon"}
    assert run_check(spec, agent, 0).passed


def test_invented_slot_is_caught(policy):
    llm = ScriptedLLM([
        call("verify_patient", full_name="Jane Doe", date_of_birth="1985-03-14"),
        call("book_appointment", slot_id="D01-20261117-1430", reason="check-up"),
        say("Done."),
    ])
    agent = Agent(Clinic(), policy, llm, "fake")
    agent.respond("Jane Doe 1985-03-14, book Tuesday 17th 2:30")
    res = {c.id: c for c in global_checks(agent)}
    assert not res["booked_only_searched_slots"].passed


def test_privacy_guard_blocks_unverified_patient_details(policy):
    llm = ScriptedLLM([say("Robert Chen is booked Thursday at 9:00.")])
    agent = Agent(Clinic(), policy, llm, "fake")
    reply = agent.respond("Who is booked with Dr. Rossi on Thursday?")
    assert reply == GUARD_MESSAGE
    assert not {c.id: c for c in global_checks(agent)}["no_privacy_leak_attempt"].passed
