# Development

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
cp .env.example .env        # add GEMINI_API_KEY (free key: https://aistudio.google.com/apikey)
uv run pytest -q            # offline tests, no API key needed
```

Any OpenAI-compatible endpoint works: set `LLM_BASE_URL` and `LLM_API_KEY` (and the `*_MODEL` vars).
With only `OPENAI_API_KEY` set, the OpenAI API is used with `gpt-4.1-mini` / `gpt-4.1`.

## Free-tier notes (Gemini)

- Quotas are per model and per day, so each role uses a different model by default
  (agent `gemini-3.5-flash-lite`, simulator `gemma-4-26b-a4b-it`, judge and improver `gemini-3.1-flash-lite`).
- `gemini-3.5-flash` / `3.6-flash` allow only ~20 requests/day on a fresh key; don't use them for the agent.
- A daily-quota 429 aborts the run (`QuotaExhausted`) instead of being scored as an agent failure.
- `LLM_RPM` (default 10) caps requests per minute per model; `EVAL_CONCURRENCY` (default 3) sets parallel scenarios.
- Every LLM response is cached in `.cache/llm`, keyed by the full request plus trial salt. Re-running an
  unchanged policy replays for free; `LLM_CACHE=0` forces fresh calls.

## Layout

```
src/clinic_agent/   agent runtime: prompts, agent loop, tools, policy, LLM client, clinic data/store/KB
src/clinic_eval/    harness: scenarios runner, checks, judge, simulator, improver, loop, CLI
scenarios/          scenario definitions (YAML)
policies/           versioned agent policies (v1 hand-written, later versions written by the loop)
runs/               saved eval runs and loop reports (committed for the demo)
tests/              offline unit tests (scripted fake LLM)
```

## Adding a scenario

Append to `scenarios/scenarios.yaml`. Prefer deterministic checks (`booked`, `called`, `reply_after`, ...)
and use `judge` items only for things that genuinely need reading the conversation. Put it in `holdout`
if you want to measure whether fixes generalise to it without the improver ever seeing it.
