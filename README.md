# Regodit AI Security Analyst

Regodit is a local, evidence-first analyst for completing vendor-security questionnaires. It searches supplied company material before asking an employee, records the provenance of every supported answer, and preserves corrections instead of silently rewriting history.

## Problem

Vendor-security reviews repeatedly ask for facts scattered across policies, assessment reports, contracts, infrastructure records, and employee knowledge. Manual completion is slow, easy to overstate, and difficult to audit because a plausible answer is not necessarily a supported answer.

## Solution

Regodit ingests the supplied evidence into stable, source-addressable records, retrieves relevant material for each questionnaire item, extracts structured claims, and decides whether to answer, ask one focused follow-up, surface a conflict, or remain unknown. Confirmed employee answers become persistent profile facts and completed questionnaires are exported as new files.

## Key Features

- Search before asking: both the security profile and document evidence are checked for every investigation.
- Evidence-backed answers with stable IDs, source paths, locations, evidence types, and organization scope.
- Intelligent, single-question follow-ups for precise missing information.
- Persistent SQLite security profile with claim and questionnaire history.
- Explicit conflict detection; contradictions are never silently averaged away.
- User corrections with supersession and an auditable claim trail.
- Confidence and evidence-quality scoring.
- OpenAI structured reasoning constrained to retrieved Regodit evidence.
- Block Convey PRISM traces for real model-backed investigations.
- Chat-first, multi-turn investigations coordinated by a persistent LangGraph thread.
- JSON and XLSX questionnaire completion without overwriting the source workbook.
- Entity relevance filtering that excludes known third-party evidence from Regodit claims.

## Architecture

```text
Company Evidence
      ↓
Ingestion
      ↓
Evidence Repository
      ↓
Retrieval
      ↓
Claim Extraction
      ↓
Analyst Reasoning
      ↕
Security Profile
      ↓
Questionnaire Completion
```

LangGraph is a thin conversation coordinator around these services: intent routing → task selection → profile/evidence investigation → answer or human interrupt → response validation → durable profile/questionnaire update. Its SQLite checkpointer stores only conversational execution state; Regodit's separate security-profile database remains the authoritative long-term knowledge store.

Business logic lives under `src/regodit`; the HTTP UI is only a presentation and API layer. Generated manifests, evidence JSONL, databases, reports, and exports live under `artifacts/`. Original files under `data/` are treated as immutable.

## Evidence Model

Regodit keeps evidence types distinct because they support different conclusions:

- `POLICY_REQUIREMENT`: what an approved policy says should happen.
- `OPERATIONAL_RECORD`: implementation or infrastructure evidence showing what is configured or performed.
- `ASSESSMENT_EVIDENCE`: observations and findings from a security assessment.
- `CONTRACTUAL_REQUIREMENT`: commitments or obligations in an agreement.
- `USER_CONFIRMATION`: a precise fact explicitly supplied by a person, retained with provenance.

Policy intent alone is not treated as proof of implementation. Claims retain the evidence IDs used to support them.

## Tech Stack

- Python 3.11+
- Python standard-library HTTP server and browser UI
- SQLite for persistent memory
- OOXML parsing/writing for DOCX and XLSX
- BM25-style lexical scoring plus character-similarity hybrid retrieval
- OpenAI Responses API with strict JSON-schema output
- Block Convey `prismtrace-sdk` observability
- LangGraph with its SQLite checkpointer for interrupt/resume conversation state
- Poppler `pdftotext` for PDF extraction when PDF files are present
- `unittest` for automated tests

The deterministic engine remains available when model or tracing configuration is absent; real AI investigations use the configured OpenAI and PRISM credentials.

## Installation

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Install Poppler separately if `pdftotext` is not already available. For example, on Ubuntu/Debian: `sudo apt install poppler-utils`.

Build all reproducible runtime artifacts from the immutable dataset:

```bash
regodit initialize
```

The command fails non-zero if a source file cannot be ingested or has an unsupported format.

## Configuration

Copy `.env.example` to `.env` and edit it, or export the variables in your shell. Regodit reads `.env` at startup from `REGODIT_PROJECT_ROOT`, the current directory, or the detected checkout, in that order; a real environment variable always wins over the file. There are no required secrets.

| Variable | Purpose | Default |
| --- | --- | --- |
| `REGODIT_PROJECT_ROOT` | Repository root | detected checkout/current directory |
| `REGODIT_DATA_DIR` | Immutable source dataset | `<root>/data` |
| `REGODIT_ARTIFACT_DIR` | Generated runtime state | `<root>/artifacts` |
| `REGODIT_EVIDENCE_PATH` | Evidence JSONL | `<artifacts>/evidence.jsonl` |
| `REGODIT_PROFILE_DB` | SQLite security profile | `<artifacts>/security_profile.sqlite3` |
| `REGODIT_CONVERSATION_DB` | LangGraph thread checkpoints | `<artifacts>/conversations.sqlite3` |
| `REGODIT_HOST` | UI bind address | `127.0.0.1` |
| `REGODIT_PORT` | UI port | `8501` |
| `LLM_PROVIDER` | Primary model provider | `openai` |
| `OPENAI_MODEL` | Structured reasoning model; the model switch | `gpt-5-mini` |
| `LLM_MODEL` | Legacy name for the same setting, used when `OPENAI_MODEL` is unset | `gpt-5-mini` |
| `OPENAI_API_KEY` | OpenAI API credential | required for model reasoning |
| `PRISMTRACE_API_KEY` | PRISM API credential | required for tracing |
| `PRISMTRACE_PROJECT_ID` | Private PRISM project identifier | required for tracing |
| `PRISMTRACE_HOST` | PRISM API base URL | required for tracing |
| `PRISMTRACE_AGENT_NAME` | Human-readable trace agent | `regodit-security-analyst` |

## AI Model

Regodit uses OpenAI `gpt-5-mini` through the Responses API by default. It was selected as the single MVP provider because it supports strict structured output with low latency and cost for focused evidence-analysis tasks. Set `OPENAI_MODEL` to another compatible OpenAI model when needed.

The model name is never hardcoded in business logic. It is resolved from configuration at startup, shown in the sidebar and on the **Model & Retest** view, and recorded on every stored evaluation.

The model interprets retrieved evidence, proposes structured claims, identifies missing information and contradictions, writes one targeted follow-up, and produces a concise answer. It receives only the active question, retrieved evidence, and persisted user-confirmed evidence—not an unrestricted search tool.

Every response must match a strict JSON schema. Regodit then independently rejects unknown evidence IDs, mismatched entities or controls, non-verbatim support text, invalid statuses, and policy evidence presented as implementation proof. Only validated claims enter the deterministic conflict, sufficiency, confidence, profile, and questionnaire logic. API, parsing, or validation failures fall back safely to the deterministic analyst.

The governing model instruction is explicit: answer only from supplied Regodit evidence and persisted user-confirmed claims; when that evidence is insufficient, return insufficient evidence instead of using outside knowledge.

## PRISM Observability

Regodit uses Block Convey PRISM through `prismtrace-sdk>=0.4.0`. LangGraph coordinates the conversation, while the native `PRISMtrace` custom client continues to instrument the genuine model call and investigation path inside `AnalystEngine`.

For each configured run PRISM receives:

- the genuine model input/output, model name, latency, and token usage;
- non-document metadata such as question ID, normalized control, evidence count, and source categories;
- a same-session trajectory covering retrieval → grounded reasoning → validated final result;
- result state, conflict presence, and whether a follow-up was required.

Tracing is enabled only when `PRISMTRACE_API_KEY`, `PRISMTRACE_PROJECT_ID`, and `PRISMTRACE_HOST` are all configured. Missing or unavailable PRISM configuration logs a warning or disables tracing without crashing the analyst. A stable agent ID and the `regodit-security-analyst` display name are used, while one `AppService` lifetime maps to one PRISM conversation session.

Generate the five dataset-backed evaluation traces with:

```bash
regodit trace-evaluation --force
```

This invokes the real application for a verified answer, missing information, an MFA conflict, Solsphere relevance filtering, and a persisted user confirmation, then explicitly flushes PRISM. Use the returned session ID to inspect the related calls in PRISM.

The Observe → Improve → Prove workflow is:

1. Run `trace-evaluation` and inspect retrieval, structured output, citations, conflicts, and follow-ups in PRISM.
2. Record one concrete observed weakness rather than guessing from an isolated payload.
3. Change the relevant prompt, retrieval, or validator and rerun the same question/session scenario.
4. Compare the validated status, citations, follow-up, latency, and token use before and after.

The dataset evaluation produced a concrete improvement story:

- **Before:** a traced `VSQ-020` run returned `UNKNOWN`, and a later MFA run let an LLM omission hide a known policy/implementation contradiction.
- **Observed:** PRISM showed the retrieval → reasoning → final-result path and highlighted limited reasoning transparency; application validation logs showed over-broad claims, unresolved fields attached to answerable output, and inconsistent entity naming. An asynchronous trajectory also exceeded its flush timeout.
- **Change:** the prompt now limits output to the three strongest question-specific claims, mandates canonical control/attribute/entity values and two-sentence answers, and forbids unresolved fields on answerable results. Validated model claims are merged with conservative extraction so omissions cannot erase conflicts. Trajectories are submitted synchronously and retain server receipt IDs.
- **After:** `VSQ-020` returned `VERIFIED` with real evidence IDs and no follow-up; the seeded MFA gap returned `CONFLICT` with a clarification question. Both receipt-backed PRISM trajectories completed with passing evaluations and an overall score of 98.89.

PRISM also reported that project-level authorized-tool and task-constraint lists were not configured. Those governance controls belong in the private PRISM project configuration; Regodit does not invent unsupported trace fields to simulate them.

## Running Regodit

After initialization, launch the application:

```bash
regodit serve
```

Open <http://127.0.0.1:8501>. The conversation is the primary workspace; Questionnaire, Security Profile, Conflicts, and Evidence are supporting views. Suggested actions start or continue prioritized investigations, and every unresolved questionnaire row can launch an evidence search in chat. Add `--analyze` to investigate all questionnaire items before serving, or use `--host`, `--port`, and `--db` for runtime overrides.

For the deterministic judging scenario:

```bash
regodit demo --force
regodit serve --db artifacts/demo_security_profile.sqlite3
```

## Switching Models and Retesting

Changing the model changes only the reasoning runtime. Evidence, the security profile, user
confirmations, questionnaire answers, conflict history, and evaluation history all survive a restart.

1. Stop the application.
2. Edit `OPENAI_MODEL` in `.env` (or export it in your shell).
3. Restart with `regodit serve`. The active model appears in the sidebar and on **Model & Retest**.
4. Re-run the questionnaire against the same evidence, from the UI or the command line.

```bash
regodit retest --scope unresolved      # default: re-runs UNKNOWN and CONFLICT rows
regodit retest --scope all             # re-runs all 66 questions, for model comparison
regodit retest --question VSQ-060      # re-runs one question
```

`python scripts/retest.py --scope unresolved` is an equivalent standalone entry point that works
without installing the package or starting the UI. Add `--json` for a full machine-readable report.

A retest never clears the questionnaire. Each evaluation is appended to a per-question history with
its model, status, confidence, evidence IDs, and timestamp, and the questionnaire shows the latest
accepted state. Retest safety follows an evidence-aware precedence: current operational evidence,
current assessment evidence, valid current user confirmation, current policy requirement, informal
observation, and last, model inference. A retest that disagrees with a user-confirmed fact is stored
as a conflict candidate rather than applied, and an `UNKNOWN → VERIFIED` upgrade that rests on no
evidence the previous run had not already seen is flagged rather than trusted.

Retests are PRISM-traced with `run_type = model_retest`, the active model, run ID, question ID,
previous status, new status, and whether a conflict was resolved.

## Questionnaire Synchronization

Every change to durable security knowledge — a clarification, a correction, a resolved conflict, or
an accepted retest — flows through one synchronizer in `src/regodit/sync.py`. It identifies every
questionnaire row mapped to the affected control, recomputes each one, and persists the result
immediately, so a single confirmation updates all related rows and chat state cannot drift away from
the questionnaire table.

Resolving a conflict supersedes the contradicted claim and records the retired assertion in an audit
ledger. Because the same assertion is otherwise re-derived from the unchanged source documents on
every investigation, that ledger is what keeps a resolved conflict resolved across restarts and
retests. The evidence itself is never deleted, and genuinely new evidence carrying the same
assertion reopens the conflict rather than being suppressed.

## Running Tests

```bash
python -m unittest discover -s tests -v
```

The suite covers questionnaire parsing, ingestion and stable evidence IDs, retrieval, claim validation, evidence sufficiency, search-before-ask behavior, entity relevance, conflict detection, persistent memory, supersession, HTTP routes, export safety, model configuration and restart safety, all three retest scopes, evaluation history, retest precedence, questionnaire synchronization, and the end-to-end demonstration.

## Demo Scenario

The deterministic demo seeds four auditable states: company evidence produces a `VERIFIED` answer; contradictory MFA claims produce `CONFLICT`; a focused clarification records an employee response; that response becomes `USER_CONFIRMED`, supersedes the conflicting profile fact, and updates the questionnaire/export while preserving its history.

## Project Structure

```text
.
├── README.md
├── BASE_MODEL_PROMPT.md
├── AGENTS.md
├── pyproject.toml
├── .env.example
├── data/                         # supplied, immutable evidence
├── scripts/retest.py             # standalone retest entry point
├── prompts/                      # governing development specifications
├── artifacts/                    # ignored, reproducible runtime outputs
├── src/regodit/
│   ├── config.py
│   ├── models/                   # shared evidence/question/claim structures
│   ├── questionnaire/            # parsing, audit, normalization
│   ├── ingestion/                # document parsing and evidence creation
│   ├── retrieval/                # hybrid evidence search and filtering
│   ├── analyst/                  # claims, reasoning, confidence, conflicts
│   ├── llm/                      # grounded OpenAI structured-output runtime
│   ├── observability.py          # fail-open PRISM tracing
│   ├── memory/                   # SQLite profile and supersession
│   ├── conversation/             # LangGraph intent, interrupts, thread state
│   ├── sync.py                   # centralized questionnaire synchronization
│   ├── retest.py                 # model retest/replay service and CLI
│   └── ui/                       # HTTP API, browser UI, XLSX export
└── tests/                        # consolidated automated coverage
```

## Limitations

- Retrieval remains deterministic and has no semantic embedding model; model reasoning begins only after evidence retrieval.
- PNG files are retained as visual evidence with provenance but are not OCR-transcribed.
- PDF extraction depends on the external `pdftotext` executable.
- The local HTTP server has no authentication and is intended for a trusted workstation/demo environment.
- Intent detection and required-field mappings are deliberately small and deterministic for the hackathon MVP; controls beyond the mapped conversational flows use the existing analyst follow-up behavior.
- Questionnaire writing targets the supplied workbook's `Vendor Security Responses` layout.
- Confidence scores rank evidence quality and consistency; they are not statistical probabilities.
- A retest re-runs the analyst against stored evidence; it does not re-ingest source documents, so new files require `regodit initialize` first.
- Questionnaire rows are mapped to controls by the normalized topic, so a claim only fans out to rows sharing that normalized control.
- Conflict candidates raised by a retest are recorded and displayed but are resolved through the existing chat clarification flow rather than a dedicated review queue.

## Future Improvements

- Add OCR for diagrams and screenshots while retaining page/image provenance.
- Add pluggable embedding retrieval and evaluated reranking for larger evidence sets.
- Support authenticated multi-user deployments and encrypted profile storage.
- Add configurable mappings for additional questionnaire workbook formats.
- Expand the evaluation corpus with reviewed ground-truth claims and retrieval judgments.
