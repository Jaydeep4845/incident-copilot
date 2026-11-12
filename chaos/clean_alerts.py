"""Drop alerts whose rule was removed (ServiceRestarted paged on routine deploys).

Alerts are evaluated passively by Prometheus and do not change the system's behaviour, so removing
a rule after recording is equivalent to never having had it. Idempotent.
"""

import json
import sys
from pathlib import Path

REMOVED = {"ServiceRestarted"}

for path in Path(sys.argv[1]).glob("*/scenario.json"):
    meta = json.loads(path.read_text())
    meta["alerts"] = [a for a in meta["alerts"] if a["alert"] not in REMOVED]
    path.write_text(json.dumps(meta, indent=2))
