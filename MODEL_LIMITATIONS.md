# Model-specific Jev limitations

Upstream-documented limitations for specific model versions; absence does not establish a fix.

Generated from [structured state](state/model-limitations.json). These describe the named version, not all present or future Jev models.

## jev-1.13 / Adversarial content

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): Untrusted state may influence decisions; specify adversarial cases in criteria and test the integration.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#adversarial-content)

## jev-1.13 / Choice option order

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): Choice results may depend on option position. Check decisions after permuting candidates.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#choice-option-order)

## jev-1.13 / Common-sense structural invariants

Status: `unknown`. Upstream last reviewed: 2026-09-17.

Repository interpretation (repository-interpretation): Similar wording does not guarantee algebraic relationships across questions. Calibrate Choice and Noul decisions separately.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#common-sense-structural-invariants)

## jev-1.13 / Contradictory instructions and criteria

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): Conflicting instructions and criteria can impair decisions; make their requirements agree.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#contradictory-instructions-and-criteria)

## jev-1.13 / Counting

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): Do not delegate tallying to this model. Classify individual candidates if needed, then aggregate programmatically.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#counting)

## jev-1.13 / Date and time comparison

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): Date text is not reliably ordered by the model. Separate semantic extraction from programmatic date calculations.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#date-and-time-comparison)

## jev-1.13 / Generation

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (unknown): Unknown: this section has no reviewed repository interpretation. Consult the official evidence.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#generation)

## jev-1.13 / Indirection

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): Nested reasoning and double negation reduce reliability. Ask directly about explicitly named state fields.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#indirection)

## jev-1.13 / Large state full of irrelevant detail

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (unknown): Unknown: this section has no reviewed repository interpretation. Consult the official evidence.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#large-state-full-of-irrelevant-detail)

## jev-1.13 / Literal reading

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): Implicit intent may not be followed. Specify exact conditions and boundary cases, splitting ambiguous decisions when needed.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#literal-reading)

## jev-1.13 / Math and Numbers

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): Reserve the model for semantic judgments; implement numerical operations programmatically.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#math-and-numbers)

## jev-1.13 / Math using score

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): A score is not an exact numerical measurement interpolated between criterion levels.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#math-using-score)

## jev-1.13 / Numeric representations

Status: `documented`. Upstream last reviewed: 2026-10-02.

Repository interpretation (repository-interpretation): Encoded numeric values are weaker inputs than semantic labels. Perform conversions outside the model before asking for judgment.

[Official evidence](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md#numeric-representations)
