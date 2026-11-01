# Jev release and substitution analysis

Reviewed 2026-10-30. This is an independently authored interpretation of public
TypeSafe evidence, not a generated tracker, benchmark, or TypeSafe endorsement.
Mutable documentation describes the current service; it does not establish when
an individual capability shipped.

## Verified identities, not an invented release delta

- The current [model page](https://docs.typesafe.ai/models.md) calls **Jev 1.14**,
  ID `jev-1.14.0`, the newest release, available from **2026-10-14**. It also lists
  `jev-1.13.0`. Both `jev-latest` and `jev-preview` currently resolve to
  `jev-1.14.0`; the page says no separate preview is available. No later model is
  documented there as of this review. These are public documentation observations,
  not an authenticated account inventory or immutable model release manifest.
- Python: [PyPI](https://pypi.org/pypi/typesafe-sdk/json) and the
  [latest GitHub release](https://api.github.com/repos/typesafe-ai/typesafe-sdk-python/releases/latest)
  agree on `typesafe-sdk` **0.7.5**. The
  [tag reference](https://api.github.com/repos/typesafe-ai/typesafe-sdk-python/git/ref/tags/v0.7.5)
  resolves to `f36e68549ceca3191e5791e5f99ea1baa1a258b5`; its immutable
  [project configuration](https://github.com/typesafe-ai/typesafe-sdk-python/blob/f36e68549ceca3191e5791e5f99ea1baa1a258b5/pyproject.toml)
  agrees on name/version and Python `>=3.10`. Its
  [changelog](https://github.com/typesafe-ai/typesafe-sdk-python/blob/f36e68549ceca3191e5791e5f99ea1baa1a258b5/docs/changelog.md)
  attributes Image, Box, `attach_assets`, and default zstd compression to 0.7.5.
- JavaScript: [npm's latest metadata](https://registry.npmjs.org/@typesafe-ai%2fsdk/latest)
  and the [latest GitHub release](https://api.github.com/repos/typesafe-ai/typesafe-sdk-js/releases/latest)
  agree on `@typesafe-ai/sdk` **0.7.0**. The
  [tag reference](https://api.github.com/repos/typesafe-ai/typesafe-sdk-js/git/ref/tags/v0.7.0)
  resolves to `b844cd1aca12855a5d24a2df52a1401c86e7d37a`; its immutable
  [package configuration](https://github.com/typesafe-ai/typesafe-sdk-js/blob/b844cd1aca12855a5d24a2df52a1401c86e7d37a/package.json)
  requires Node `>=22`. Its
  [changelog](https://github.com/typesafe-ai/typesafe-sdk-js/blob/b844cd1aca12855a5d24a2df52a1401c86e7d37a/docs/changelog.md)
  establishes experimental images and Box, `image()` / `Image.fromBase64()`,
  `withAssets`, and zstd for bodies of at least 1 KiB on Node `>=22.15`.
  It also removes request-payload type exports; this is not a drop-in runtime-only upgrade.
- The [official skill head](https://api.github.com/repos/typesafe-ai/skills/commits/main)
  is `302eea662c54bca6730873fe245bec1d69ebc23d` (2026-10-23).
  Its [plugin manifest](https://github.com/typesafe-ai/skills/blob/302eea662c54bca6730873fe245bec1d69ebc23d/.claude-plugin/plugin.json)
  says **0.6.2**; that is a plugin version, not a model or SDK version.
  The immutable [skill](https://github.com/typesafe-ai/skills/blob/302eea662c54bca6730873fe245bec1d69ebc23d/skills/typesafe-ai/SKILL.md)
  is guidance, not an API schema or proof of model accuracy.

Tag bindings, registry responses, release metadata, aliases, and hosted pages can
move. Commit-addressed source above pins the reviewed SDK/skill content; it does
not prove byte-for-byte registry artifact reproducibility. Neither current model
names nor SDK release dates establish a general accuracy improvement from 1.13
to 1.14. In particular, multilingual support, images, and long context must not
all be advertised as newly introduced in 1.14: the current image page explicitly
supports images on both versions, and the model page supports long context on both.

## Capability adoption matrix

Here, **adopt** means adopt the engineering pattern, not enable an untested model
in production. **Evaluate** means a candidate needs representative labeled quality
and end-to-end cost/latency evidence. **Defer** means do not use this substitution
for the proposed job. No row below reports a new model-quality evaluation.

| Capability / workload eligible now | Exact generative step removable | Evidence quality and release boundary | Cost / latency implications | Decision |
| --- | --- | --- | --- | --- |
| Exact lookups, counting parser matches, arithmetic, date ordering | Entire LLM decision or calculation call; use a parser, lookup, or ordinary code, not another model | [1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md) and pinned skill explicitly favor code; no new model capability needed | No replacement inference request; application savings still require a measured displaced baseline | **Adopt** deterministic exits first |
| Closed-set intent, category, candidate/span selection | Prompt producing a label or JSON, textual parsing, and format-repair generation | [Choice](https://docs.typesafe.ai/primitives/choice.md): selected option, probabilities, confidence; up to 255 options. Existing typed programming model, not a verified 1.14 introduction | One typed request replaces the narrow generation; candidate discovery and fallback can dominate | **Evaluate**; code checks candidate coverage and handles no match |
| Independent yes/no conditions, including whether any candidate fits | Generated yes/no answers or a broad model-written policy decision | [Noul](https://docs.typesafe.ai/primitives/noul.md) returns P(yes), no separate confidence. Pinned skill recommends companion Noul for null cases as of 1.14; this is guidance, not a benchmark | Bundle shared-state checks; adding a verifier to every request without removing a call adds expense | **Evaluate** atomic judgments; **adopt** policy/truth table in code |
| Rubric scoring, relevance/ranking signals | LLM-generated numeric rubric or prose justification that code only converts into a score | [Score](https://docs.typesafe.ai/primitives/score.md): 2–10 described levels, probability-weighted position and distributions. Not precise numeric extraction | Reuse scores when only code weights change; extra questions still consume tokens | **Evaluate** one dimension per question; keep exact math in code |
| Bounded value/date extraction from complete candidate sets | Free-form generation/retyping of a source value, followed by parsing/repair | [Pre-parsed extraction](https://docs.typesafe.ai/cookbooks/pre_parsed_value_extraction_cookbook.md) demonstrates regex → Choice → copy/normalize; example uses 1.14, not a proof of a new primitive | Candidate generation can be deterministic; omitted candidates cannot be recovered by Choice; dependent requests add latency | **Evaluate** selection; **adopt** exact source copying and validation |
| Non-English/CJK bounded judgments | Translate-then-classify generation, or general LLM classification, only if direct judgment proves sufficient | [Models](https://docs.typesafe.ai/models.md): English strongest; other languages handled unequally. No per-language accuracy table or verified 1.13→1.14 multilingual delta | Translation disappears only for accepted direct results; retries and fallback can erase savings | **Evaluate** separately by language/domain; defer translation removal until measured |
| Image classification/comparison using Choice/Noul/Score | Vision LLM generating a bounded label or semantic observation | [Images](https://docs.typesafe.ai/images.md) explicitly supports both 1.13 and 1.14, with per-request `TypeSafe-Danger-Zone-Images: true`; marked not production-ready | Images count as input tokens at text's rate; no published estimator yet; repeated references expand token/size usage | **Evaluate** prototypes only, not a production-ready 1.14 upgrade claim |
| Single-target image localization / crop selection | Vision LLM generating coordinates and coordinate-format repair | [Box](https://docs.typesafe.ai/primitives/box.md) plus Images: Box needs 1.14; one normalized rectangle per question, no criteria, no not-found answer | Add presence Noul; small targets may need tiles/crops and another request; nullable confidence prevents assuming a universal gate | **Evaluate** prototypes; defer production substitution |
| Requests beyond normal context limits | A preliminary summarization/chunk-merging generation step, only when the resulting direct judgment is accurate enough | [Models](https://docs.typesafe.ai/models.md): defaults 64k request / 32k per question; header `TypeSafe-Danger-Zone-Long-Context: true` raises both to 262k for both versions | Same token price, explicitly slower and less accurate; filtering first may be cheaper and better | **Defer** as default; **evaluate** exceptional necessary long inputs |
| Several independent judgments sharing state | Repeated label-generating calls, or repeated TypeSafe round trips; no free-form answer generation is added | [Skill](https://github.com/typesafe-ai/skills/blob/302eea662c54bca6730873fe245bec1d69ebc23d/skills/typesafe-ai/SKILL.md) and [batching recipe](https://docs.typesafe.ai/cookbooks/parallel_questions.md). Questions cannot see other answers | State paid once; speculative questions still cost tokens. Separate calls remain necessary when earlier answers construct later state | **Adopt** bounded shared-state composition; **evaluate** end-to-end budgets |
| SDK/skill adoption | SDK removes handwritten transport/serialization; skill can improve design, but neither inherently removes a generative invocation | Immutable SDK changelogs above establish client features, not model parity; current skill keeps judgment separate from execution | Compression reduces wire bytes, not billed input tokens. Connection reuse can help transport latency; no measured benefit here | **Adopt** compatible typed clients and reviewed guidance; no automatic savings claim |
| Open-ended prose, explanations, code generation, multi-hop reasoning | No suitable generative step eliminated by these bounded primitives | 1.13 limitation page rejects generation; no reviewed 1.14 evidence establishes a general generative contract | Chaining choices to synthesize text adds calls and is not a useful replacement | **Defer** substitution; retain the appropriate generative system |

## Contract caveats that change the decision

The [current HTTP reference](https://docs.typesafe.ai/api/system-one.md) declares
`POST /v1/systemone`, required `model`, `state`, and nonempty named `questions`,
optional `assets`, and answers correlated by the same names. An SDK's documented
default alias is not an HTTP default. Pin the versioned model when comparing
results or calibrating thresholds; aliases may move independently of application code.

There are meaningful documentation differences to preserve, not flatten:

- The model overview's input table says Text, while the dedicated Images page
  explicitly enables experimental images on both versions. Use the narrower image
  contract with its opt-in and readiness warning; do not infer general audio/video support.
- Box prose shows numeric confidence, but the HTTP schema allows **number or null**,
  explicitly when the model supplies no confidence. Treat missing certainty as
  unknown, never as a confident detection. Box always returns coordinates even for
  an absent target; a presence judgment must gate consumption.
- The immutable JavaScript 0.7.0 changelog says to use `jev-preview` for Box; current
  model/image docs say Box needs 1.14 and both aliases now target it. The old release
  instruction is not evidence of a separate preview model today.
- The [confidence guide](https://docs.typesafe.ai/confidence.md) describes
  Choice/Score confidence as distribution concentration. The pinned skill cautions
  against using it as a universal correctness gate. These fit together: evaluate
  probabilities, distributions, and question-specific abstention, not one global
  confidence cutoff. Noul 0.3/0.7 examples are starting points, not calibrated guarantees.
- [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md), last
  reviewed 2026-10-02, covers literal readings, numbers/counting, dates, indirection,
  irrelevant context, adversarial content, contradictory instructions, option order,
  and generation. No 1.14-specific limitation page is listed in the current
  [official index](https://docs.typesafe.ai/llms.txt). Neither fixed nor unchanged
  1.14 failure modes can be established from that absence. Retain conservative
  evaluation cases without relabeling 1.13 evidence as a measured 1.14 limitation.

Images require PNG/JPEG stills, 280–1568 pixels on each side, up to 16 MiB per file;
body, processed-image aggregate, and expanded-request limits are each 32 MiB.
Repeated asset references count repeatedly after expansion. Rejected images fail
the whole request, not just one answer. The [image guide](https://docs.typesafe.ai/images.md)
and [request rules](https://docs.typesafe.ai/api/errors.md) are the current sources
for exact limits and errors. Compression does not increase the decompressed limit.

## Savings accounting and evaluation boundary

The current model page lists both versions at **$0.042 per million input tokens**,
with output tokens free. Thus 10,000 input tokens imply **$0.00042** at that listed
rate: a calculated example, not observed spend. Image tokens are included in input
usage; no quantitative latency guarantee or 1.13→1.14 speedup is established here.

The upstream batching recipe reports 13 single calls versus one batch at
$0.006090 / 2.71 s versus $0.000497 / 0.27 s. Its code pins **`jev-1.12`**, uses
five repeats over one article, and sums **sequential** single-call latency. Its
12.2×/10.0× results are an upstream example, not a 1.14 benchmark, general model
quality result, concurrent baseline comparison, or savings observed by this project.

For a proposed replacement, separate:

1. **Contract evidence:** the typed interface and supported input exist.
2. **Policy replay:** fixtures test routing, abstention, and accounting; supplied
   answers do not evaluate a model.
3. **Shadow quality:** labeled held-out representative inputs establish accuracy,
   error severity, abstention, candidate recall, and language/image slices; shadow
   calls add cost and displace nothing.
4. **Observed substitution:** accepted bounded results actually skip the original
   generation. Count displaced calls, replacement and fallback cost, retry/error
   rates, and end-to-end p50/p95 latency against a comparable baseline.

If the original call costs G, the substitute costs J, and fraction a actually
avoids the original, expected model-only savings are `a*G - J`, before other
costs. This is an estimate, not a measured result. A verifier that never suppresses
generation has `a = 0`. Deterministic exits should precede either model. This review
made no billable inference calls and reports no local model-quality measurements,
observed substitution savings, or measured monetary/latency improvements.

## Relationship to the tracker

The generated model, SDK and skill identities already cover these reviewed
versions. Source coverage includes images, Box, confidence, model limitations,
and the new HTTP reference page. No duplicate release tracker or fabricated
history is needed. Generated files remain owned by synchronization.

Discovery coverage is not semantic extraction parity: `state/api.json` currently
marks contract claims unknown because the older `docs:api` reference is missing;
the newer `api/system-one` page uses an OpenAPI-shaped declaration. This analysis
links current official contracts directly and does not upgrade those generated
claims or relabel mutable SDK changelog observations as immutable release facts.
