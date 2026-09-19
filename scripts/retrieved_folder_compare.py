"""Compatibility shim — the module now lives in the mct package."""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mct.retrieved_folder_compare import *  # noqa: F401,F403
from mct.retrieved_folder_compare import (  # noqa: F401
    TreeCompareResult,
    compare_trees,
    index_tree,
    norm_key,
    text_equal_ignoring_line_endings,
)
