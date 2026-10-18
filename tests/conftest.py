import copy

import pytest

from jev_docs import sync
from jev_docs.practice_reviews import practice_context


@pytest.fixture
def review_contexts(monkeypatch):
    """Explicitly review authored fixture snapshots; never approve mutated inputs implicitly."""
    registry = copy.deepcopy(sync.REVIEWED_PRACTICE_CONTEXTS)
    monkeypatch.setattr(sync, "REVIEWED_PRACTICE_CONTEXTS", registry)

    def approve(contents, artifacts, verdicts):
        for sid, text in contents.items():
            _context, digest = practice_context(text)
            artifact = artifacts[sid]
            registry.setdefault(sid, {})[digest] = {
                "source_url": artifact.url,
                "source_sha256": artifact.sha256,
                "verdicts": dict(verdicts[sid]),
            }

    return approve
