"""Attach bounded workflow phase metadata without publishing logs or source bodies."""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

path = Path(sys.argv[1])
attempt = json.loads(path.read_text()) if path.exists() else {}
attempt.setdefault("attempt_timestamp", datetime.now(UTC).isoformat())
steps = json.loads(os.environ.get("STEP_OUTCOMES", "{}"))
attempt["failure_phase"] = next(
    (name for name, value in steps.items() if value.get("outcome") == "failure"), "setup"
)
baseline = os.environ.get("GITHUB_SHA", "")
attempt["canonical_baseline_commit"] = baseline if re.fullmatch(r"[0-9a-f]{40}", baseline) else None
attempt["result"] = "failure"
path.write_text(json.dumps(attempt, indent=2, sort_keys=True) + "\n")
