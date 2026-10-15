# Small end-to-end financial evaluation

This directory compares FinanceAgent with a **native Codex general-purpose agent**, not the ChatGPT product. Both receive the same natural-language financial request and use the provider/model configured in the repository `.env`. Codex receives no financial API hints, attached filings, golden answers, or FinanceAgent tools.

## Scope and methodology

- `cases.json`: three initial tasks covering AAPL/MSFT FY2024–FY2025, 120 required numerical cells.
- `holdout-cases.json`: two additional prompts checking calendar alignment and a universal growth claim. These reuse the companies and financial periods, so they are transfer checks, not an independent unseen-company benchmark.
- `golden.json`: 16 fiscal quarters and four annual rows, independently verified against 16 original company earnings statements. It includes source URLs, statement locations, calculation operands, comparative checks and explanatory evidence. It is **not** copied from FinanceAgent's database or SEC companyfacts.
- `rubric.json`: fixed numeric tolerances and a five-dimension assistant analysis rubric (20 points). Numeric correctness, answer completeness, analysis quality, runtime and token usage are reported separately.
- Each case/engine has a 300-second task limit, no runner-level automatic retry, fresh conversation and preserved failure records.
- FinanceAgent uses a separate evaluation user, bootstrapped agent, credential and session per case. Shared public-data caches are not cleared.
- Codex uses a fresh home/workspace per case and Full Access. **An empty directory is not a filesystem sandbox.** Use an isolated machine/container for untrusted evaluations; do not run this on a host containing unrelated sensitive resources.

Assistant analysis scoring is subjective, not independent human annotation. Review bundles remove engine labels, but formatting can reveal the engine, so this is not a strictly blind study. Missing telemetry is unknown, not zero. Provider input-token totals include cached context where the provider reports it; they are not equivalent to monetary cost.

## Setup

The app must already be running. On Windows this project uses WSL2 Docker for the backend. The Node runners use the real HTTP chat service; they do not call internal finance functions as a substitute for the application.

```sh
npm ci --prefix eval
node --test eval/score.test.mjs
```

Node 24+ is required. The runner reads `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` and `DEEPSEEK_MODEL` from the repository `.env`; it does not print secrets. These variable names reflect this project's current configuration, not a claim that Codex supports every DeepSeek-compatible endpoint.

FinanceAgent preflight verifies the actual stored credential and model against `.env` in memory. Select an existing idle source session with the intended complete model parameters. The runner copies those parameters to **new evaluation sessions** and disables fallback; it does not rewrite the source session. The October 2026 comparison used an existing source configuration with `parameters: {}` to avoid adding an output cap only to FinanceAgent.

## Execute

Dry runs do not start models:

```sh
node eval/run-codex.mjs --case all
node eval/run-finance.mjs --preflight --source-session SOURCE_SESSION_ID
```

Use new output paths for every attempt. Existing case results must not be overwritten. Finance's output root must not already exist; its parent must exist.

```sh
node eval/run-codex.mjs --run --case all --timeout 300 \
  --output /absolute/path/to/initial/codex

node eval/run-finance.mjs --run --case all --timeout 300 \
  --source-session SOURCE_SESSION_ID \
  --output /absolute/path/to/initial/finance
```

Codex accepts `--codex-path /absolute/path/to/codex` to pin a CLI executable and `--sdk-path /absolute/path/to/sdk/dist/index.js` for an already installed SDK. Record both versions; the initial experiment used SDK 0.162.1 with CLI 0.156.0. The default installed SDK may bundle a different CLI version, so do not mix versions within a comparison without recording the change.

For the two additional prompts, add `--cases eval/holdout-cases.json`. After a product change, rerun FinanceAgent into a new root instead of replacing the original results. Preserve initial failures and note that same-question retests are development results.

At timeout FinanceAgent sends the native interrupt request and waits for idle; it stops the batch if shutdown cannot be confirmed. Codex uses the SDK cancellation signal; incomplete turns retain available telemetry snapshots and partial responses. A runtime completion signal alone does not prove that the user task or a saved report succeeded.

## Numeric scoring

Normalize only values actually delivered to the user into an `answers.json`:

```json
{
  "cells": [{
    "company": "AAPL",
    "fiscal_year": 2025,
    "fiscal_quarter": 2,
    "period_end": "2025-03-29",
    "metric": "revenue",
    "value": 95359,
    "evidence": "response.md:12: verbatim table row",
    "provenance_supported": null
  }]
}
```

Amounts are normalized to USD millions; `net_margin` is percentage points, and `free_cash_flow_to_net_income` is a decimal ratio. For annual rows omit `fiscal_quarter`. Preserve raw response excerpts or delivered artifact/cell references. Never fill omissions from the golden data. A saved report card is a delivered answer even when the final chat only confirms its location; unpublished failed drafts are not.

```sh
node eval/score.mjs --case quarterly-profit \
  --answers /absolute/path/to/answers.json \
  --output /absolute/path/to/numeric-score.json
```

The scorer separates conditional accuracy from coverage and correct coverage, checks explicitly supplied quarter ends, and rejects contradictory duplicate values. If the answer gives contradictory units, record `unit_conflict: true` with the conflicting excerpt: a matching printed number is not a correct financial value when its unit is ambiguous. This implements the original rubric's unit-matching requirement; it does not widen or change numeric tolerances. `provenance_supported` is set only after a separate evidence review; unresolved support stays `null`. Numerical agreement alone does not establish source support or validate the explanation.

## Analysis review and evidence

```sh
node eval/build-review-bundle.mjs \
  --runs /absolute/path/to/initial \
  --output /absolute/path/to/review
```

Review every delivered answer with the fixed rubric. Deductions must identify an excerpt and a concrete counterexample or unmet requirement. Preserve the scores and reasoning, including unsupported claims and cases where an answer is numerically correct but its report was not saved.

Raw responses, SDK events, native session traces, user-accessible artifacts and per-call usage belong under the git-ignored `data/eval-runs/`. Versioned summaries and review decisions accompany the evaluation report in `docs/评估.md`.
