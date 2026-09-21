# V1 design

## Evidence boundary

The canonical web discovery input is `https://docs.typesafe.ai/llms.txt`.
The synchronizer follows same-host links from that index and records the public
sitemap's `lastmod` values when available. If the index is unavailable, the
sitemap is a fallback; if discovery cannot be established, the run fails and
does not remove old state. Mintlify's `.md` representation is used for Markdown
fetches, with the normal page form as a bounded fallback when a Markdown
response is unavailable.

GitHub and package registries are separate immutable-or-mutable provenance
streams. A released SDK is identified by package version, matching Git tag, and
tag commit when all are available. `main` is captured as current source context
but is never substituted for a release. Python and JavaScript state is never
merged into one version stream.

Hosted documentation is not bulk mirrored. There is no documentation license in
the discovered legal or repository surfaces that would justify doing so. V1
retains metadata, hashes, source URLs, and short matching excerpts used by
derived claims. Repository code is MIT-licensed; that does not relicense
TypeSafe documentation.

The organization inventory was checked during bootstrap. `skills`,
`typesafe-sdk-python`, and `typesafe-sdk-js` are included. The
`system-one-adapter-python` project is directly relevant but excluded because
it is an LLM-backed alternative rather than canonical TypeSafe guidance.
`typesafe-ai.github.io` is a static landing page and is excluded. LLaDA,
Overwatch, daggerverse, pulumi-clickhouse, vllm, and other organization
projects are unrelated to the Jev/System One documentation evidence boundary.

## Generated layers

- `sources/` contains one manifest per source family.
- `state/` contains stable JSON snapshots with explicit schema versions and
  provenance.
- `state/source-coverage.json` records discovered, fetched, retained,
  intentionally excluded, newly discovered, and disappeared pages. Successful
  discovery sets `removal_safe`; failed discovery never promotes a replacement
  coverage snapshot.
- `BEST_PRACTICES.md` is rendered only from `state/practices.json`.
- `events/` contains stable-ID semantic changes. `events/baseline.json` marks
  the initial snapshot without manufacturing additions.
- `changes/` renders the events for a day. Existing historical reports are not
  regenerated; only the current day's report can receive additional events.

The generated state uses stable JSON ordering and preserves an artifact's first
observation timestamp while its content hash is unchanged. This avoids clock-
driven churn while retaining meaningful observation timestamps. Events use a
SHA-256 identity over schema, entity, change type, and before/after hashes.

## Practices

V1 uses an explicit table of conservative extraction rules in
`src/jev_docs/sync.py`. Each rule has a stable ID, category, status, summary,
and one or more source patterns. A rule becomes `recommended` only when current
canonical text matches; absent evidence becomes `unknown`, not deprecated.
Matching excerpts are retained in the derived practice item so a reader can
audit the interpretation without downloading the full source.

## Failure and freshness semantics

Discovery and required source fetches happen before any repository output is
promoted. A failed run exits nonzero and leaves generated state untouched. This
is the last-known-good guarantee. GitHub Actions runs strict validation and a
12-hour schedule with concurrency protection; it can push only generated
changes using the standard token. Freshness reports whether the observed package
metadata, tags, skill commit, and docs discovery agree at the time of a
successful run. The first SDK release discrepancy is observed with a three-day
grace period; if the same discrepancy remains after that period, strict
synchronization fails before promotion. A transient fetch failure remains an
immediate failed run and does not update freshness.

## Deferred work

V1 does not reconstruct a mutable hosted-doc timeline, mirror full pages, run
AST compatibility analysis, ingest community material, or infer deprecation from
absence. Those features require stronger evidence and should remain separate
from the canonical layer.
