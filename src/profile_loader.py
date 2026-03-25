from __future__ import annotations

from pathlib import Path
from typing import Dict, Any

import yaml


def load_profile(path: str) -> Dict[str, Any]:
    p = Path(path)
    return yaml.safe_load(p.read_text(encoding="utf-8"))
