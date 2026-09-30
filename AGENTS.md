---
purpose: Project rules for AI agents
updated: 2026-09-28
source: generated-by-zcode-starterkit
---

# AGENTS.md

## Purpose

TradingAgents — a multi-agent LLM financial trading framework (research tool): LangGraph-orchestrated analyst/researcher/trader/risk agents over market data vendors, shipped as a Python library + Typer CLI.

## Source of Truth

1. This `AGENTS.md`
2. Repo-local docs (`README.md`, `CHANGELOG.md`, `.env.example`, `pyproject.toml` comments)
3. `.zcode/memory/project/tech-stack.md`
4. Code and tests
5. Baseline / external catalogs only when they match this repo's stack or surface

## Stack Snapshot

- **Language:** Python ≥ 3.10 (CI matrix: 3.10–3.13; local venv runs 3.13)
- **Framework:** LangGraph + LangChain provider clients; Typer/Rich CLI
- **Package Manager:** pip, setuptools backend, no lockfile (`pip install -e ".[dev]"`)
- **Detected Shape:** Python library + CLI (`tradingagents = cli.main:app`), Docker optional

## Core Coding Contract

- Read repo instructions, docs, configs, and nearby code before editing.
- Prefer existing patterns and the smallest correct diff.
- Preserve current public APIs, data shapes, and external side effects only when they are part of the current requirements; do not add backward-compatibility layers, migrations, or fallbacks for obsolete behavior.
- Do not add dependencies, frameworks, broad refactors, or generated churn unless the task requires them.
- Run the repo's actual relevant commands after meaningful changes: `.venv\Scripts\python.exe -m ruff check .` and `.venv\Scripts\python.exe -m pytest -q` (see Verified Commands).
- Self-review the diff, including untracked files, and remove debug leftovers before completion.
- Report skipped verification with reasons instead of claiming unverified success.

## Engineering Principles

1. Do not preserve backward compatibility. Delete obsolete code directly; do not add compatibility layers, write migrations, or leave fallbacks.
2. Choose the simplest implementation that satisfies the current requirements. Avoid speculative abstractions and unnecessary configuration layers.
3. Keep the system layered for the long term. First make a minimal end-to-end version work, then add complexity. Never dismantle working code for unfinished complexity.
4. Keep components modular and separate concerns.
5. Prefer mature, actively maintained libraries. Do not rewrite established capabilities without a clear reason.
6. Inspect what existing dependencies can already do before adding packages or writing custom code. Do not assume a library is unavailable.
7. Make architecture decisions for the long term. Do not accept temporary solutions framed as "we can replace this later."
8. First study how mature products solve the same problem and use proven patterns; do not invent from scratch.

## Coding Standards (apply strictly)

- **Source:** LLM Wiki cross-language cookbook (`C:\Users\mrc\Documents\projects\agent-wiki`) + Python deep cookbook. Reopen via the `obsidian` skill for detail.

### Shared rules across languages
- Prefer the repo's own conventions over generic style guidance.
- Use the ecosystem's canonical formatter first, then linter, then tests. Here: `ruff check` (formatter NOT yet adopted repo-wide — do not run `ruff format` on the whole repo).
- Keep the smallest possible diff that satisfies the requested behavior.
- Match naming, file layout, import/order conventions, and module boundaries already used in the repo.
- Do not invent abstractions, helpers, or architecture unless the repo requires them.
- Preserve public APIs and behavior unless a breaking change is explicitly approved.
- Add or update tests for every behavior change.
- Keep comments factual and sparse.
- Treat errors, edge cases, and validation as part of the standard, not an afterthought.
- Verify before finalizing: linting, tests (and type checks when applicable).

### Structural rules common to all languages (mandatory)
- **One file = one responsibility.** A file is one module named after a single concern; if the name needs "and"/"or" to be honest, it has too many. Cohesion wins over colocation by type.
- **Split signals (any one fires a mandatory split proposal):**
  1. Multi-role identity — the file answers more than one "what does this do?" question.
  2. Section-header navigation — you scroll past unrelated sections to reach your change.
  3. Unrelated pile-up — new code is unrelated to the file's primary concern.
  4. Cross-domain import surface — imports from many unrelated subsystems.
  5. Repeated edits in different places by unrelated tasks.
  6. God symbol — one class/function/file handles multiple input domains or output shapes.
- Split at the responsibility seam, not a line count. A 200-line file serving two domains needs splitting; a 1500-line single-concern file may be fine.
- **Anti-patterns to refuse on sight:** `utils`/`helpers`/`common`/`misc` catch-all modules; grab-bag public surface; giant regression test files instead of mirroring the source split; "just one more function" on a module already showing split signals.
- **Repo-first override:** the repo's existing module-boundary convention wins over heuristics. This repo groups by what modules hold: `tradingagents/{agents, dataflows, graph, llm_clients}` + `cli/` + `tests/`.

### Escalation covenant (mandatory agent behavior)
Before finalizing any coding pass, scan the structure of every file the diff touches. If any split signal fires:
1. Surface a split proposal in the same turn, naming the proposed seam.
2. Pause for the user to decide. Do not silently refactor someone else's repo, and do not silently leave a violation either.
3. If declined, record `# ai-note: split declined because <reason>; revisit when signal X strengthens` at the file head.
4. If approved, perform the split in the same pass when practical, run lint/tests, and report the new boundaries.

### Python-specific (from the deep cookbook; repo wins on conflicts)
- No bare `except:`; catch specific types; chain with `raise NewError(...) from exc` when translating (see `_apply_env_overrides`).
- Imports at top, grouped stdlib → third-party → local; absolute imports; ruff `I` enforces order.
- Modules stay importable with no side effects; guard `main()`/CLI entrypoints.
- Naming: `lower_with_under` functions/modules, `CapWords` classes, `CAPS_WITH_UNDER` constants, `Error` suffix on real exceptions.

## Selected Guideline Packs

- **Primary (strong):** LLM Wiki `queries/coding-standards-cross-language-cookbook.md` + `queries/coding-standards-programming-languages-python-cookbook.md` (vault: `C:\Users\mrc\Documents\projects\agent-wiki`).
- **Secondary:** bundled `agent-skills-standard` (pinned `9f695e8`, 2026-08-01) file-matched packs: `python/python-language`, `python/python-tooling`, `python/python-testing`, `python/python-architecture`.
- **Ignored:** TypeScript/JavaScript/frontend/mobile/database packs (no such surfaces in this repo).

## Stack-Specific Rules

- Tests use pytest markers `unit` / `integration` / `smoke` (`--strict-markers`); new markers must be registered in `pyproject.toml`. Unit tests must not hit the network — mock vendors/LLMs; live-API tests skip themselves when keys are absent.
- CI also runs the suite under `TZ=America/New_York`: never write timezone-dependent assertions; resolve dates with explicit `zoneinfo`/`pytz` zones.
- Always pass `encoding="utf-8"` when opening text files (Windows default is cp1252; this project fixed UTF-8 bugs before).
- New env-overridable config keys go in `_ENV_OVERRIDES` in `tradingagents/default_config.py` — coercion follows the default's type; no entry-point changes needed.
- Never introduce look-ahead in dated data paths: a run dated in the past must read data as it stood that day (point-in-time integrity is a core v0.5 contract).
- Check Python version compat: code must run on 3.10–3.13, so no 3.11+-only syntax in `tradingagents/`.

## Repo-Specific Rules

- Import paths moved in v0.5.1: modules are grouped by what they hold (`agents/`, `dataflows/`, `graph/`, `llm_clients/`). Check `tradingagents/__init__.py` / existing imports before adding new ones.
- `ruff check .` must stay clean; the repo deliberately ignores `E501` and defers repo-wide `ruff format` — do not mass-reformat.
- Vendor/data-vendor chains (`data_vendors` config) fall through in order; keep the fallback chain contract when touching dataflows.
- The CLI entry is `cli/main.py` (Typer); user-facing state lives under `~/.tradingagents/` (logs, cache, memory log, checkpoints) — never write run state into the repo.
- `results/` is generated output, excluded from ruff; don't edit or commit it.

## Boundaries / Gotchas

- API keys come from env / `.env` only (see `.env.example`); never commit keys or hardcode endpoints.
- `.venv/` is local (already gitignored); use `.venv\Scripts\python.exe` on Windows rather than global Python.
- LLM behavior is non-deterministic; tests assert structure/config plumbing, not model output.
- This is a research framework — not financial advice; keep disclaimers intact in user-facing output.

## Verified Commands

```bash
.venv\Scripts\python.exe -m pip install -e ".[dev]"   # fresh env setup
.venv\Scripts\python.exe -m ruff check .               # lint (strict select, full repo)
.venv\Scripts\python.exe -m pytest -q                  # full suite (1001 passed, 5 skipped on Windows 2026-09-28)
.venv\Scripts\python.exe -c "import tradingagents, cli.main"   # clean-import smoke (mirrors CI)
tradingagents --help                                   # CLI (installed entry point)
```

## Code Example

```python
# tradingagents/default_config.py — the env-var → config-key mapping is a table.
# To expose a config key, add a row here; coercion follows the existing default's type.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER": "llm_provider",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "max_debate_rounds",
    ...
}

def _apply_env_overrides(config: dict) -> dict:
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce(raw, config.get(key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
    return config
```

## Synthesis Notes

- **Rule translation:** wiki cross-language structural rules + Python cookbook error/import rules were folded into Coding Standards and Stack-Specific Rules; CI (`ci.yml`) supplied the verified command set and the TZ matrix gotcha; `pyproject.toml` comments supplied the "no repo-wide ruff format" rule.
- **Source notes:**
  - Local wiki (vault `C:\Users\mrc\Documents\projects\agent-wiki`): `queries/coding-standards-cross-language-cookbook.md` (Structural rules / Escalation covenant / Shared rules), `queries/coding-standards-programming-languages-cookbook.md`, `queries/coding-standards-programming-languages-python-cookbook.md`. Reopen via the `obsidian` skill (`read_note` / `search_notes`).
  - Bundled catalog: `agent-skills-standard` pinned commit `9f695e8e2c3e423dfcd420a0e6b80e0e99044088` (snapshot 2026-08-01, Apache-2.0) — packs listed under Selected Guideline Packs.
- **Open questions:** none blocking; `/review-codebase` can deepen subsystem notes.
