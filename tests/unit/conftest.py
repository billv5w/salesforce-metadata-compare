"""Shared fixtures for unit tests.

Centralises the importlib dance needed to import ``env-compare.py``
(hyphenated filename) and adds ``scripts/`` to ``sys.path`` so that
sibling modules like ``retrieved_folder_compare`` are importable.
"""
import importlib.util
import os
import sys

# Ensure scripts/ is on sys.path for sibling imports (xml_normalizer, retrieved_folder_compare, etc.)
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# env-compare.py uses a hyphen so we need importlib to load it as a module.
_spec = importlib.util.spec_from_file_location(
    "env_compare",
    os.path.join(_SCRIPTS_DIR, "env-compare.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["env_compare"] = _mod
_spec.loader.exec_module(_mod)
