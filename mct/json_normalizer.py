"""
JSON normalizer for SFDX metadata — reduces false-positive diffs in JSON configuration files.

Recursively sorts keys and canonicalizes indentation (4 spaces). Array order is
NEVER changed: it is semantic (e.g. component order in JSON metadata) — the same
stance as the embedded-JSON canonicalization in xml_normalizer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _normalize_value(val: Any) -> Any:
    if isinstance(val, dict):
        # Recursively normalize nested structures
        return {k: _normalize_value(v) for k, v in val.items()}
    elif isinstance(val, list):
        # Array order is semantic (e.g. component order in JSON metadata) —
        # same stance as embedded-JSON canonicalization in xml_normalizer.
        return [_normalize_value(x) for x in val]
    else:
        return val


def normalize_json(text: str) -> str:
    """
    Parse *text* as JSON, recursively normalize structure, sort keys, and format nicely.

    Falls back to the original *text* if not parseable as JSON.
    """
    # Normalize line endings to avoid CRLF differences in text nodes
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text  # Safe fallback for malformed or non-JSON files

    normalized = _normalize_value(data)
    # dumps with sort_keys=True sorts all keys alphabetically
    return json.dumps(normalized, indent=4, sort_keys=True, ensure_ascii=False) + "\n"


def json_semantically_equal(lp: Path, rp: Path) -> bool:
    """
    Return True if *lp* and *rp* represent the same JSON after normalization.
    """
    try:
        lt = lp.read_text(encoding="utf-8", errors="replace")
        rt = rp.read_text(encoding="utf-8", errors="replace")
        return normalize_json(lt) == normalize_json(rt)
    except Exception:
        return False
