"""Compatibility shim — the module now lives in the mct package."""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mct.json_normalizer import *  # noqa: F401,F403
from mct.json_normalizer import json_semantically_equal, normalize_json  # noqa: F401
