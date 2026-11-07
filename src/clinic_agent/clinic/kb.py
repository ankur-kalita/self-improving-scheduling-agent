"""Clinic knowledge base + tiny lexical retriever.

Policy facts (fees, hours, insurance) must come from here, never from the
model's priors. The corpus is small enough that keyword scoring is reliable and
fully inspectable; a vector store would add nothing but nondeterminism.
"""

from __future__ import annotations

import re

KB = [
    {
        "id": "hours",
        "title": "Opening hours",
        "keywords": "hours open opening close closing time weekend saturday sunday lunch holiday",
        "text": "The clinic is open Monday to Friday, 9:00 to 17:00, closed 12:00-13:00 for lunch. "
        "Closed Saturdays, Sundays and public holidays.",
    },
    {
        "id": "location",
        "title": "Address, parking and access",
        "keywords": "address where location directions parking park car bus wheelchair access",
        "text": "120 Maple Grove Avenue, Springfield. Free parking in the lot behind the building. "
        "Step-free wheelchair access via the main entrance. Bus line 14 stops outside.",
    },
    {
        "id": "cancellation",
        "title": "Cancellation and rescheduling policy",
        "keywords": "cancel cancellation reschedule move change fee charge late no-show penalty policy 24 hours",
        "text": "Appointments can be cancelled or rescheduled free of charge up to 24 hours before the start time. "
        "Changes made less than 24 hours before the appointment incur a $50 late-change fee. "
        "Missed appointments without notice (no-shows) incur a $75 fee.",
    },
    {
        "id": "insurance",
        "title": "Insurance",
        "keywords": "insurance insurer coverage covered plan aetna cigna bluecross medicare medicaid humana self-pay cost price",
        "text": "Accepted: Aetna, BlueCross BlueShield, Cigna, UnitedHealthcare and Medicare. "
        "Not accepted: Humana and Medicaid. Self-pay standard visit: $140. "
        "Coverage questions beyond this list go to the billing office at 555-0150.",
    },
    {
        "id": "new_patients",
        "title": "New patients",
        "keywords": "new patient first visit register registration bring documents id card arrive early",
        "text": "New patients should arrive 15 minutes early and bring photo ID, their insurance card "
        "and a list of current medications.",
    },
    {
        "id": "visit_types",
        "title": "Visit types and specialties",
        "keywords": "specialty specialties doctor physical checkup check-up skin rash mole child kid pediatric family dermatology duration long",
        "text": "Family medicine (Dr. Priya Shah, Dr. Hannah Kim): check-ups, annual physicals, chronic conditions, minor illness. "
        "Pediatrics (Dr. Marcus Lee): patients under 18; a parent or guardian must attend. "
        "Dermatology (Dr. Elena Rossi, Thursdays only): skin, moles, rashes. All visits are 30 minutes.",
    },
    {
        "id": "prescriptions",
        "title": "Prescription refills",
        "keywords": "prescription refill medication medicine pharmacy renew drug dose",
        "text": "Refill requests go through the patient portal or the patient's pharmacy and take 2 business days. "
        "The scheduling assistant cannot issue, change or advise on prescriptions.",
    },
    {
        "id": "nurse_line",
        "title": "Nurse advice line",
        "keywords": "nurse advice medical question symptom symptoms worried should i dose side effect",
        "text": "For non-emergency medical questions, the nurse advice line is available 24/7 at 555-0199.",
    },
    {
        "id": "emergency",
        "title": "Emergencies",
        "keywords": "emergency urgent chest pain breathing stroke bleeding suicide overdose 911 er",
        "text": "For a medical emergency call 911 or go to the nearest emergency room. The clinic does not handle emergencies.",
    },
    {
        "id": "billing",
        "title": "Billing",
        "keywords": "bill billing invoice payment pay charge statement refund",
        "text": "Billing questions: billing office 555-0150, Monday to Friday 9:00-17:00.",
    },
]

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(_TOKEN.findall(s.lower()))


def search_kb(query: str, k: int = 2) -> list[dict]:
    q = _tokens(query)
    scored = []
    for doc in KB:
        kw = _tokens(doc["keywords"]) | _tokens(doc["title"])
        body = _tokens(doc["text"])
        score = 2 * len(q & kw) + len(q & body)
        if score:
            scored.append((score, doc))
    scored.sort(key=lambda x: -x[0])
    return [{"id": d["id"], "title": d["title"], "text": d["text"]} for _, d in scored[:k]]
