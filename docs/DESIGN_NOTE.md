# Design note

**Key design choices.** (1) *Code enforces anything code can enforce.* Identity is bound to the session by
`verify_patient`, and tools never take a patient ID, so a prompt-injected model still can't reach another
patient's data. The store handles double booking, overlaps, atomic reschedules and the late-cancellation fee.
Writes carry idempotency keys so retries are safe. A deterministic output guard blocks other patients'
names and dates of birth. (2) *The model handles only what needs language:* what to ask, read-backs,
phrasing, and when to escalate. (3) *A fixed world:* a frozen clock and seeded calendar, so the only thing
that changes between runs is the agent. (4) *Two kinds of graders.* Deterministic checks on the end
database state and the tool trace catch what a transcript-only judge can't see: "you're booked" with no
booking, booking a slot that never appeared in a search, a leak masked by the guard. The judge covers what
code can't: whether the agent read back details, refused dosing advice, or was honest about an outage.
A critical rubric item the judge marks "na" counts as a fail.

**How the loop works.** Run 16 scenarios (10 train, 6 holdout; happy paths plus emergency, impostor,
injection, timeouts, slot race, duplicate, ambiguous date, proxy booking for a child). The improver sees
only train failures (transcript, tool trace, failed checks) and returns a typed proposal: a diagnosis with
its root-cause layer (prompt, tool note, runtime, code, or eval), policy edits, code proposals for a human,
and eval concerns where the simulator or judge was at fault. A validator rejects out-of-bounds runtime
values and overfit rules (patient names, dates, IDs). The candidate re-runs every scenario. The gate
accepts it only if the train score rises and no scenario regresses, after re-running suspected
regressions with fresh trials to rule out noise. Accepted policies are saved as `policies/vN.yaml`.

**Before and after.** v1 baseline: **6/16 scenarios pass (0.38), mean score 0.882** (train 0.40 / 0.860,
holdout 0.33 / 0.919). The main real failure: the agent booked as soon as the patient picked a slot, with
no read-back. Two others: a near-advice slip on medication dosing, and a duplicate booking because it never
checked existing appointments. The improver diagnosed the read-back failure across six scenarios on its
own and proposed a single general rule. *After (v2): pending; the run is in progress and will appear in
`runs/*-loop/report.md`.*

**One production change.** Replace name and date of birth as the only identity factor with a one-time code
sent to the phone on file before any write. Name and DOB are guessable and often public.

**AI vs. my judgment.** AI wrote most of the code and ran the patient simulator, judge and improver. I
overrode it on these points:
- Kept security limits out of the improver's action space.
- Dropped plain "is scheduled" from the false-success regex. It flagged honest descriptions of existing
  appointments.
- Dropped a text check in the injection scenario that fired on "I can't confirm whether X is booked".
- Made daily-quota errors abort the run instead of being scored as agent failures.
- Kept a holdout set the improver never sees.
- Checked the judge's read-back failures against the transcripts before letting the loop act on them.
- Didn't weaken v1 to manufacture a failure.

Known blind spots: one trial per scenario (noisy), the simulator is an LLM and can drift, and the judge
shares a model family with the agent.
