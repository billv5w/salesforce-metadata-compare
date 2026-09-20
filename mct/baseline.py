"""Baseline: persisted ignore rules and accepted-diff fingerprints.

Every org has permanent, legitimate drift (org-specific settings, apiVersion
churn, untracked types). Without a way to record "this is known and accepted",
every compare re-surfaces the same diffs and the report is permanently red.

The baseline lives per project at ``STORAGE_ROOT/baseline.json``:

{
  "version": 1,
  "ignore": {
    "types": ["reports"],              // first path segment, case-insensitive
    "paths": ["objects/Account_Legacy*"],  // fnmatch globs on display path
    "xml_elements": ["apiVersion",         // local XML element names stripped
                     "omniScripts:isActive"]  // from BOTH sides before comparison;
                                       // "type:element" scopes the rule to files
                                       // whose first path segment matches type
  },
  "accepted": {
    "<display path>": {"fingerprint": "sha256...", "accepted_at": "...", "note": ""}
  }
}

Ignored and accepted diffs are classified separately — never silently dropped.
An accepted diff whose content changes (fingerprint mismatch) resurfaces as
active drift flagged ``accepted_stale``.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import mct.config as _cfg

_BASELINE_FILENAME = "baseline.json"


def default_baseline() -> dict[str, Any]:
    return {
        "version": 1,
        "ignore": {"types": [], "paths": [], "xml_elements": []},
        "options": {"strip_retrieve_defaults": False},
        "accepted": {},
    }


def baseline_path() -> Path:
    return _cfg.STORAGE_ROOT / _BASELINE_FILENAME


def load_baseline(path: Path | None = None) -> dict[str, Any]:
    """Load the baseline; malformed or missing files yield an empty baseline."""
    p = path or baseline_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_baseline()
    if not isinstance(data, dict):
        return default_baseline()
    base = default_baseline()
    ignore = data.get("ignore") or {}
    for key in ("types", "paths", "xml_elements"):
        vals = ignore.get(key)
        if isinstance(vals, list):
            base["ignore"][key] = [str(v).strip() for v in vals if str(v).strip()]
    options = data.get("options") or {}
    if isinstance(options, dict):
        base["options"]["strip_retrieve_defaults"] = bool(options.get("strip_retrieve_defaults"))
    accepted = data.get("accepted")
    if isinstance(accepted, dict):
        base["accepted"] = {
            str(k): v for k, v in accepted.items() if isinstance(v, dict) and v.get("fingerprint")
        }
    return base


def strip_retrieve_defaults(baseline: dict[str, Any]) -> bool:
    return bool(baseline.get("options", {}).get("strip_retrieve_defaults"))


def save_baseline(data: dict[str, Any], path: Path | None = None) -> Path:
    from mct.index import _atomic_write

    p = path or baseline_path()
    _atomic_write(p, data, sort_keys=True)
    return p


def xml_ignore_elements(baseline: dict[str, Any]) -> frozenset[str]:
    """Globally ignored XML element names (entries without a "type:" scope)."""
    return frozenset(e for e in baseline["ignore"]["xml_elements"] if ":" not in e)


def xml_ignore_by_type(baseline: dict[str, Any]) -> dict[str, frozenset[str]]:
    """Scoped "type:element" entries as {type_lower: {elements}}.

    The scope is the file's first path segment (same matcher as
    ``ignore.types``), so e.g. "omniScripts:isActive" suppresses <isActive>
    churn in omniScripts/ without hiding it everywhere else.
    """
    out: dict[str, set[str]] = {}
    for entry in baseline["ignore"]["xml_elements"]:
        type_part, sep, element = entry.partition(":")
        if sep and type_part.strip() and element.strip():
            out.setdefault(type_part.strip().lower(), set()).add(element.strip())
    return {t: frozenset(els) for t, els in out.items()}


def effective_xml_ignore(display_path: str, baseline: dict[str, Any]) -> frozenset[str]:
    """Global ignores plus any scoped to *display_path*'s first path segment."""
    from mct.retrieved_folder_compare import effective_ignore_for

    return effective_ignore_for(
        display_path, xml_ignore_elements(baseline), xml_ignore_by_type(baseline)
    ) or frozenset()


def ignore_reason(display_path: str, baseline: dict[str, Any]) -> str | None:
    """Why *display_path* is ignored ('type:...' / 'path:...'), or None."""
    dp = display_path.replace("\\", "/")
    top = dp.split("/")[0].lower()
    for t in baseline["ignore"]["types"]:
        if top == t.lower():
            return f"type:{t}"
    for pattern in baseline["ignore"]["paths"]:
        if fnmatch.fnmatch(dp.lower(), pattern.lower()):
            return f"path:{pattern}"
    return None


class FingerprintReadError(RuntimeError):
    """A file could not be read for fingerprinting.

    An unreadable file is never treated as empty or as accepted content —
    silently hashing "" would let a vanished file keep a stale acceptance.
    """


_FINGERPRINT_SCHEME = "v2"


def compute_fingerprint(
    lp: Path | None,
    rp: Path | None,
    ignore: frozenset[str] | None = None,
    strip_defaults: bool = False,
    managed_namespaces: frozenset[str] | None = None,
    *,
    left_root: Path | None = None,
    right_root: Path | None = None,
) -> str:
    """Content fingerprint of a differing file pair (either side may be absent
    for only-left/only-right entries).

    Hash input is the file's raw bytes — never lossy decoded text — so
    distinct binary contents can never collide (v1 decoded everything as
    UTF-8-with-replacement, colliding e.g. ``00 82`` and ``00 83``). Text
    canonicalisation is applied only where the comparison engine applies
    the same equivalence rule:

      - ``.xml``   → ``normalize_xml`` (as ``xml_semantically_equal``;
                     pass the same *managed_namespaces* the comparison ran
                     with so accepted fingerprints cover the view the user
                     actually saw)
      - ``.json``  → ``normalize_json`` (as ``json_semantically_equal``)
      - text types → CRLF→LF + trailing-whitespace strip, on bytes exactly
                     as ``text_equal_ignoring_line_endings``
      - all others → raw bytes (the engine byte-compares them)

    Existence and content boundaries are unambiguous: each side contributes
    a ``missing``/``norm``/``raw`` tag plus an explicit length, so
    missing-vs-empty, empty-vs-missing and swapped sides all differ.

    Scheme version ``v2`` is embedded in both the digest preimage and the
    returned string, so fingerprints stored by older versions can never
    match — accepted entries become ``accepted_stale`` and resurface until
    re-accepted, without corrupting the baseline (notes/ignore rules are
    untouched).

    A fingerprint is a read boundary: linked files are never hashed. A
    direct link always fails; when *left_root*/*right_root* are given the
    full trusted-root policy applies, so a link on ANY ancestor beneath
    the comparison root is rejected as well. The roots act as the anchors
    captured at comparison time — pass the canonical ``TreeCompareResult``
    roots so a replaced root or ancestor cannot re-anchor trust.
    """
    from mct.json_normalizer import normalize_json
    from mct.retrieved_folder_compare import (
        LinkedMetadataError,
        _TEXT_SUFFIXES,
        read_metadata_bytes,
    )
    from mct.xml_normalizer import normalize_xml

    def canon(p: Path | None, root: Path | None) -> tuple[bytes, bytes]:
        """(tag, payload) for one side."""
        if p is None:
            return b"missing", b""
        try:
            if p.is_symlink():
                raise LinkedMetadataError(
                    f"Cannot fingerprint a symbolic link: {p}"
                )
            data = (
                read_metadata_bytes(root, p, anchor=root)
                if root is not None
                else p.read_bytes()
            )
        except (OSError, LinkedMetadataError) as exc:
            raise FingerprintReadError(
                f"Cannot fingerprint unreadable or unsafe file {p}: {exc}"
            ) from exc
        suffix = p.suffix.lower()
        if suffix == ".xml":
            return b"norm", normalize_xml(
                data.decode("utf-8", errors="replace"), ignore,
                strip_defaults, managed_namespaces,
            ).encode("utf-8")
        if suffix == ".json":
            return b"norm", normalize_json(
                data.decode("utf-8", errors="replace")
            ).encode("utf-8")
        if suffix in _TEXT_SUFFIXES:
            # Same byte-level canon as text_equal_ignoring_line_endings.
            return b"norm", data.replace(b"\r\n", b"\n").rstrip(b"\r\n\t ")
        return b"raw", data

    ltag, ldata = canon(lp, left_root)
    rtag, rdata = canon(rp, right_root)
    h = hashlib.sha256()
    h.update(_FINGERPRINT_SCHEME.encode("ascii"))
    for tag, payload in ((ltag, ldata), (rtag, rdata)):
        h.update(b"\x00")
        h.update(tag)
        h.update(b"\x00")
        h.update(str(len(payload)).encode("ascii"))
        h.update(b"\x00")
        h.update(payload)
    return f"{_FINGERPRINT_SCHEME}:{h.hexdigest()}"


def pair_key_for(
    left_info: dict[str, Any] | None, right_info: dict[str, Any] | None
) -> str | None:
    """Stable identity of a comparison pair (branch:<name> or org:<alias> per
    side). None when either side is a plain path — acceptances then stay global."""

    def ctx(info: dict[str, Any] | None) -> str | None:
        if not info:
            return None
        if info.get("type") == "branch" and info.get("branch"):
            return f"branch:{info['branch']}"
        if info.get("org_alias"):
            return f"org:{info['org_alias']}"
        return None

    lc, rc = ctx(left_info), ctx(right_info)
    if lc is None or rc is None:
        return None
    return f"{lc}↔{rc}"


def accept_diff(
    display_path: str,
    lp: Path | None,
    rp: Path | None,
    note: str = "",
    baseline_file: Path | None = None,
    pair_key: str | None = None,
    managed_namespaces: frozenset[str] | None = None,
    *,
    left_root: Path | None = None,
    right_root: Path | None = None,
) -> dict[str, Any]:
    """Record *display_path*'s current diff as accepted; returns the entry.

    With *pair_key* the acceptance applies only to that comparison pair;
    without it (legacy / plain-path comparisons) it applies everywhere.
    Pass the *managed_namespaces* the comparison ran with so the stored
    fingerprint covers the same normalized view the user accepted.
    """
    from mct.index import locked_update

    target = baseline_file or baseline_path()
    data = load_baseline(target)
    entry = {
        "fingerprint": compute_fingerprint(
            lp, rp, effective_xml_ignore(display_path, data),
            strip_retrieve_defaults(data), managed_namespaces,
            left_root=left_root, right_root=right_root,
        ),
        "accepted_at": _cfg.ts_local(),
        "note": note,
    }
    if pair_key is not None:
        entry["pair"] = pair_key

    def _add(d: dict[str, Any]) -> dict[str, Any]:
        d.setdefault("accepted", {})[display_path] = entry
        return entry

    return locked_update(target, default_baseline, _add, sort_keys=True)


def unaccept_diff(display_path: str, baseline_file: Path | None = None) -> bool:
    from mct.index import locked_update

    def _del(d: dict[str, Any]) -> bool:
        acc = d.get("accepted") or {}
        if display_path in acc:
            del acc[display_path]
            return True
        return False

    return locked_update(
        baseline_file or baseline_path(), default_baseline, _del, sort_keys=True
    )


def run_baseline_command(args: Any) -> int:
    """CLI entry point for the ``baseline`` subcommand (show/add/remove/clear)."""
    cmd = getattr(args, "baseline_cmd", "")
    if cmd == "show":
        data = load_baseline()
        if getattr(args, "as_json", False):
            print(json.dumps(data, indent=2, sort_keys=True))
            return 0
        print(f"Baseline: {baseline_path()}")
        ig = data["ignore"]
        print(f"  Ignored types:        {', '.join(ig['types']) or '(none)'}")
        print(f"  Ignored path globs:   {', '.join(ig['paths']) or '(none)'}")
        print(f"  Ignored XML elements: {', '.join(ig['xml_elements']) or '(none)'}")
        print(f"  Strip retrieve defaults: {'on' if strip_retrieve_defaults(data) else 'off'}")
        accepted = data["accepted"]
        print(f"  Accepted diffs:       {len(accepted)}")
        for path, entry in sorted(accepted.items()):
            note = f"  — {entry['note']}" if entry.get("note") else ""
            print(f"    {path}  (accepted {entry.get('accepted_at', '?')}){note}")
        return 0

    if cmd in ("add-ignore", "remove-ignore"):
        additions = {
            "types": [t.strip() for t in (args.type or []) if t.strip()],
            "paths": [p.strip() for p in (args.path or []) if p.strip()],
            "xml_elements": [e.strip() for e in (args.xml_element or []) if e.strip()],
        }
        if not any(additions.values()):
            print("✗ Nothing to change: pass --type / --path / --xml-element.", file=sys.stderr)
            return 1
        data = load_baseline()
        for key, values in additions.items():
            current = data["ignore"][key]
            if cmd == "add-ignore":
                for v in values:
                    if v not in current:
                        current.append(v)
            else:
                data["ignore"][key] = [v for v in current if v not in values]
        save_baseline(data)
        print(f"✓ Baseline updated: {baseline_path()}")
        return 0

    if cmd == "set":
        value = getattr(args, "strip_retrieve_defaults", None)
        if value not in ("on", "off"):
            print("✗ Pass --strip-retrieve-defaults on|off.", file=sys.stderr)
            return 1
        data = load_baseline()
        data["options"]["strip_retrieve_defaults"] = value == "on"
        save_baseline(data)
        print(f"✓ strip_retrieve_defaults = {value}. "
              "Elements equal to their Metadata-API default (e.g. <trackHistory>false</…>) "
              "are now " + ("suppressed" if value == "on" else "compared") + ".")
        return 0

    if cmd == "clear-accepted":
        data = load_baseline()
        if getattr(args, "all", False):
            n = len(data["accepted"])
            data["accepted"] = {}
        else:
            paths = [p for p in (args.path or []) if p]
            if not paths:
                print("✗ Pass --path DISPLAY_PATH (repeatable) or --all.", file=sys.stderr)
                return 1
            n = 0
            for p in paths:
                if p in data["accepted"]:
                    del data["accepted"][p]
                    n += 1
        save_baseline(data)
        print(f"✓ Removed {n} accepted entr{'y' if n == 1 else 'ies'}.")
        return 0

    print(f"✗ Unknown baseline command: {cmd}", file=sys.stderr)
    return 1


def classify_entry(
    display_path: str,
    lp: Path | None,
    rp: Path | None,
    baseline: dict[str, Any],
    pair_key: str | None = None,
    managed_namespaces: frozenset[str] | None = None,
    *,
    left_root: Path | None = None,
    right_root: Path | None = None,
) -> tuple[str, str | None]:
    """Classify one drift entry (differ pair, or only-left/only-right with the
    missing side passed as None) against the baseline.

    Returns (status, detail) where status is one of:
      - "active"          — real, unbaselined drift
      - "ignored"         — matches an ignore rule (detail = rule)
      - "accepted"        — accepted and content unchanged (detail = accepted_at)
      - "accepted_stale"  — accepted but content changed since (resurfaces)

    An acceptance carrying a "pair" scope only matches the same *pair_key*;
    pair-less acceptances (legacy) match every comparison.
    """
    reason = ignore_reason(display_path, baseline)
    if reason:
        return "ignored", reason
    entry = baseline["accepted"].get(display_path)
    if entry and entry.get("pair") is not None and entry.get("pair") != pair_key:
        entry = None  # accepted for a different comparison pair — not here
    if entry:
        try:
            current = compute_fingerprint(
                lp, rp, effective_xml_ignore(display_path, baseline),
                strip_retrieve_defaults(baseline), managed_namespaces,
                left_root=left_root, right_root=right_root,
            )
        except FingerprintReadError:
            # Unreadable side — the stored fingerprint cannot be verified,
            # so the acceptance must not silently apply; resurface as stale.
            return "accepted_stale", entry.get("accepted_at")
        if current == entry.get("fingerprint"):
            return "accepted", entry.get("accepted_at")
        return "accepted_stale", entry.get("accepted_at")
    return "active", None
