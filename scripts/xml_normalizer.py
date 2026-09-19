"""Compatibility shim — the module now lives in the mct package."""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mct.xml_normalizer import *  # noqa: F401,F403
from mct.xml_normalizer import (  # noqa: F401
    _MAX_XML_BYTES,
    normalize_xml,
    normalize_xml_lines,
    xml_semantically_equal,
)
