"""Apply the CODE_FLAGS rule to recordings made before it existed (idempotent)."""

import json
import sys
from pathlib import Path

CODE_FLAGS = {"CURRENCY_BUG", "AUDIT_LEAK_KB"}

for path in Path(sys.argv[1]).glob("*/deploys.json"):
    deploys = json.loads(path.read_text())
    for d in deploys:
        d["config_changed"] = sorted(set(d["config_changed"]) - CODE_FLAGS)
    path.write_text(json.dumps(deploys, indent=2))
