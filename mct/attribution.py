"""Drift attribution: who changed what, when — via SetupAuditTrail (read-only SOQL)."""
from __future__ import annotations

import json
from typing import Any

import mct.config as _cfg

_QUERY = (
    "SELECT CreatedDate, CreatedBy.Name, Action, Section, Display "
    "FROM SetupAuditTrail WHERE CreatedDate = LAST_N_DAYS:{days} "
    "ORDER BY CreatedDate DESC LIMIT 2000"
)


def run_attribute(
    org_alias: str, since_days: int, name_filters: list[str], as_json: bool
) -> int:
    """Print recent SetupAuditTrail entries, optionally filtered by component name."""
    from mct.safety import run as _run_safe

    result = _run_safe(
        ["sf", "data", "query", "--query", _QUERY.format(days=max(1, since_days)),
         "--target-org", org_alias, "--json"],
        capture=True, cwd=str(_cfg.PROJECT_ROOT),
    )
    records = (json.loads(result.stdout or "{}").get("result") or {}).get("records") or []
    needles = [n.lower() for n in name_filters if n.strip()]
    entries: list[dict[str, Any]] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        display = str(r.get("Display") or "")
        if needles and not any(n in display.lower() for n in needles):
            continue
        entries.append({
            "when": r.get("CreatedDate"),
            "who": (r.get("CreatedBy") or {}).get("Name"),
            "action": r.get("Action"),
            "section": r.get("Section"),
            "display": display,
        })
    if as_json:
        print(json.dumps({"org": org_alias, "since_days": since_days,
                          "entries": entries}, indent=2))
    else:
        if not entries:
            print(f"No SetupAuditTrail entries in the last {since_days} day(s)"
                  + (f" matching {', '.join(name_filters)}" if name_filters else "") + ".")
        for e in entries:
            print(f"  {e['when']}  {e['who'] or '?':<24} {e['section'] or '-':<20} {e['display']}")
        print("\nNote: SetupAuditTrail covers Setup-driven changes; API/CLI deploys "
              "appear as the deploying user's 'changed' entries. Not all metadata "
              "types are audited.", flush=True)
    return 0
