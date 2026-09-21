# AGENTS.md

## Repository purpose

`jev-docs` is an unofficial, community-maintained record of Jev / TypeSafe
System One evidence. It tracks canonical API and documentation observations,
SDK and agent-guidance provenance, derived engineering practices, and
append-safe semantic history. It does not speak for or represent TypeSafe.

## Source-of-truth rules

- Live TypeSafe documentation is authoritative for current documentation
  guidance.
- Git commits and tags are preferred for immutable SDK and skill provenance.
- Do not attribute a mutable web observation to an SDK release without matching
  package, tag, and commit evidence.
- Keep observed evidence separate from derived interpretation.
- Unknown stays unknown; do not manufacture history, semantics, or removals.

## Important invariants

- Synchronization must be deterministic and idempotent.
- Failed discovery or fetch must not imply removals or replace last-known-good
  evidence.
- Stable semantic event IDs must deduplicate retries.
- Baseline establishment must not generate misleading historical additions.
- A practice disappearing from a source does not automatically mean it is
  deprecated or removed.
- Generated output must not churn because of timestamps or volatile metadata.
- Public-source tracking must not require `TYPESAFE_API_KEY` or billable
  inference.
- Never commit credentials, authorization headers, cookies, local paths, or
  private data.

## Generated versus authored files

The synchronizer owns `sources/`, `state/`, `events/`, `changes/`, and
`BEST_PRACTICES.md`. Change these through the synchronizer or its rendering
rules, not by hand, unless repairing an explicitly identified generated-state
issue. `events/baseline.json` is generated bootstrap history and must retain
its special baseline semantics.

`README.md`, `docs/DESIGN.md`, `src/`, `tests/`, `justfile`, `.github/`,
`pyproject.toml`, and this file are authored project files. Inspect the
implementation and current design docs if the repository structure changes.

## Development workflow

Use the repository's `justfile` commands:

- `just setup` installs development dependencies with `uv`.
- `just lint` runs Ruff lint and format checks.
- `just test` runs pytest.
- `just typecheck` runs Python bytecode compilation checks.
- `just validate` validates the generated tree without strict freshness rules.
- `just check` runs lint, tests, compilation, and normal validation.
- `just sync` observes public sources and generates repository state.
- `just check-strict` runs `check` plus strict generated-state validation.

`just check-strict` does not implicitly run standalone workflow validation.
Run `actionlint` separately when changing GitHub Actions workflows. Avoid live
sync work for changes unrelated to synchronization unless fresh evidence is
specifically needed.

## Before committing

- Run relevant tests and validation for the files changed.
- For synchronization changes, verify strict validation and deterministic,
  idempotent generated output.
- Run `actionlint` for workflow changes.
- Review `git diff`, generated-state changes, and `git status`.
- Use DCO-signed commits: `git commit -s`.

## Scope discipline

V1 intentionally excludes speculative community evidence, reconstruction of a
mutable hosted-document timeline, AST-level SDK compatibility analysis,
unsupported deprecation inference, and bulk hosted-document mirroring without
licensing confidence. Do not expand those areas incidentally.

Prefer small, evidence-backed changes that fix concrete correctness or
resilience issues. Do not import complexity from other repositories unless
this repository actually needs it.
