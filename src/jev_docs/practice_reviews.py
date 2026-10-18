"""Authored reviews of complete official practice contexts, not extracted polarity.

Only hashes, source identities and per-recommendation review verdicts are stored.
Changed contexts remain unreviewed until an evidence-backed code review updates this data.
"""

import hashlib
import re

PRACTICE_EVIDENCE_POLICY = "reviewed-contexts-v1"


def practice_context(content: str) -> tuple[str, str]:
    """Preserve complete source structure, normalizing only documented whitespace forms."""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    context = "\n".join(line.rstrip() for line in content.split("\n")).strip("\n")
    context = re.sub(r"\n{3,}", "\n\n", context)
    return context, hashlib.sha256(context.encode()).hexdigest()


# Manually reviewed complete source snapshots, including metadata, quotes and code.
# "non_conflicting" is not recommendation evidence; it records a reviewed source
# that does not oppose the recommendation. No rule/source pair is approved by default.
REVIEWED_PRACTICE_CONTEXTS = {
    "docs:concepts/how-to-build-with-system-one": {
        "d7d46c748922c06da57817c4595385cba59b1db5d32829a019d7d35bebae688e": {
            "source_sha256": "9f5a20ac39383695d554bf812b365ed4b70622b8e5ac10484dce290469c68972",
            "source_url": "https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md",
            "verdicts": {
                "deterministic-before-jev": "non_conflicting",
                "diagnose-failure-layers": "non_conflicting",
                "narrow-coherent-questions": "supported",
            },
        }
    },
    "docs:confidence": {
        "04d3afbc5bc6d3e581a38fd542a602b2385fa38f6de022c7e59c5e4f76e2ccec": {
            "source_sha256": "5c325ec0c0b69e78129406ca7c5d3aef113bce365abd34985b8674cd278c81be",
            "source_url": "https://docs.typesafe.ai/confidence.md",
            "verdicts": {
                "choice-confidence-concentration": "non_conflicting",
                "confidence-is-not-permission": "supported",
                "domain-calibrated-thresholds": "non_conflicting",
            },
        }
    },
    "docs:introduction/coding-agents": {
        "84ae8185b6d5087f55ceab82f8dca26f9616b1341214f6c69f78ca24a6e42386": {
            "source_sha256": "72b81af8a34379d9c6d0f632aabc6b69b98dad6a85371ffd088687e844841a80",
            "source_url": "https://docs.typesafe.ai/introduction/coding-agents.md",
            "verdicts": {
                "jev-complements-generative-agents": "supported",
                "prefer-typed-decisions-over-prompt-parsing": "supported",
            },
        }
    },
    "docs:primitives/noul": {
        "c4de48b07e555ea3effbb6f466d6ea848fec78cc7c419212531fbe1f6a87442f": {
            "source_sha256": "bf5d8c6700fbd9272ce27b53a6439d598e7915cdf505704ac7c827c6175ecb88",
            "source_url": "https://docs.typesafe.ai/primitives/noul.md",
            "verdicts": {"noul-is-yes-probability": "supported"},
        }
    },
    "skill:typesafe-ai": {
        "577c72513fdaaec11fb5bd5cdbabb19fcc5cefc4664a8cc25307a1ef5853b289": {
            "source_sha256": "71ea90d7906c6554c4f4c460ef7361b2d26f59116ccdae986dc6d997b9389f52",
            "source_url": "https://raw.githubusercontent.com/typesafe-ai/skills/65a39f393687675ce170e6094757de20370365b9/skills/typesafe-ai/SKILL.md",
            "verdicts": {
                "batch-independent-questions": "supported",
                "candidate-must-be-present": "supported",
                "choice-confidence-concentration": "supported",
                "confidence-is-not-permission": "non_conflicting",
                "deterministic-before-jev": "supported",
                "diagnose-failure-layers": "supported",
                "domain-calibrated-thresholds": "supported",
                "explicit-speculative-premises": "supported",
                "narrow-coherent-questions": "non_conflicting",
                "noul-is-yes-probability": "non_conflicting",
                "observed-state-distinct-from-inference": "supported",
                "typed-output-is-not-truth": "supported",
            },
        }
    },
}
