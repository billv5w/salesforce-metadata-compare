"""
XML normalizer for SFDX metadata — reduces false-positive diffs.

Converts XML to a canonical form so that formatting differences (indentation,
element ordering, attribute ordering, CRLF vs LF) do not appear as meaningful
changes when comparing org retrieves against branch source.

Never modifies source files; used only for comparison and diff generation.

Key field mapping derived from:
  https://github.com/scolladon/sf-git-merge-driver/blob/main/src/service/MetadataService.ts
"""

from __future__ import annotations

import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Salesforce metadata namespace
# ---------------------------------------------------------------------------

_SF_NS = "http://soap.sforce.com/2006/04/metadata"
_SF_NS_PREFIX = f"{{{_SF_NS}}}"

# Register as default namespace so serialised output uses xmlns="..." cleanly.
ET.register_namespace("", _SF_NS)


def _local(tag: str) -> str:
    """Strip namespace prefix from an ET tag name."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


# ---------------------------------------------------------------------------
# Per-element-tag sort-key field mapping
# Source: sf-git-merge-driver MetadataService.ts
# ---------------------------------------------------------------------------

# Maps the *local* tag name of a repeating child element to the local name(s)
# of the child element(s) that form its unique identity key.
# A list means a composite key (values joined with ".").
_ELEMENT_KEY_FIELDS: dict[str, str | list[str]] = {
    # CustomLabels
    "labels": "fullName",
    # Profile / PermissionSet
    "applicationVisibilities": "application",
    "categoryGroupVisibilities": "dataCategoryGroup",
    "classAccesses": "apexClass",
    "customMetadataTypeAccesses": "name",
    "customPermissions": "name",
    "customSettingAccesses": "name",
    "dataspaceScopes": "dataspaceScope",
    "emailRoutingAddressAccesses": "name",
    "externalCredentialPrincipalAccesses": "externalCredentialPrincipal",
    "externalDataSourceAccesses": "externalDataSource",
    "fieldPermissions": "field",
    "flowAccesses": "flow",
    "layoutAssignments": ["layout", "recordType"],  # composite
    "loginFlows": "friendlyname",
    "objectPermissions": "object",
    "pageAccesses": "apexPage",
    "profileActionOverrides": "actionName",
    "recordTypeVisibilities": "recordType",
    "tabVisibilities": "tab",
    "userPermissions": "name",
    # SharingRules
    "sharingCriteriaRules": "fullName",
    "sharingGuestRules": "fullName",
    "sharingOwnerRules": "fullName",
    "sharingTerritoryRules": "fullName",
    "criteriaItems": ["field", "operation", "value", "valueField"],  # composite (ORDERED — booleanFilter numbers positions)
    # CustomField
    "filterItems": ["field", "operation", "value", "valueField"],  # composite (ORDERED)
    "summaryFilterItems": ["field", "operation", "value", "valueField"],  # composite (ORDERED)
    "valueSettings": "valueName",
    "value": "fullName",
    # Workflow
    "alerts": "fullName",
    "recipients": "type",
    "fieldUpdates": "fullName",
    "flowActions": "fullName",
    "flowInputs": "name",
    "knowledgePublishes": "fullName",
    "outboundMessages": "fullName",
    "rules": "fullName",
    "actions": "name",
    "send": "fullName",
    "tasks": "fullName",
    # AssignmentRules / AutoResponseRules / EscalationRules
    "assignmentRule": "fullName",
    "autoResponseRule": "fullName",
    "escalationRule": "fullName",
    # MatchingRules
    "matchingRules": "fullName",
    "matchingRuleItems": ["fieldName", "matchingMethod"],  # composite
    # MarketingAppExtension
    "marketingAppExtActions": "apiName",
    "marketingAppExtActivities": "fullName",
    # GlobalValueSet / StandardValueSet
    "customValue": "fullName",
    "standardValue": "fullName",
    # Translations / CustomObjectTranslation
    "valueTranslation": "masterLabel",
    "caseValues": ["article", "caseType", "plural", "possessive"],  # composite
    "fieldSets": "name",
    "fields": "name",
    "picklistValues": "masterLabel",
    "values": "fullName",
    "layouts": "layout",
    "recordTypes": "name",
    "sharingReasons": "name",
    "standardFields": "name",
    "validationRules": "name",
    "webLinks": "name",
    "workflowTasks": "name",
    # Package.xml
    "types": "name",
    # OmniStudio (OmniScript / OmniIntegrationProcedure / OmniDataTransform).
    # Document order is not semantic for these: element order is encoded in
    # sequenceNumber/*Sequence fields, and retrieves shuffle them freely.
    "omniProcessElements": "name",
    "omniDataTransformItem": ["inputFieldName", "outputFieldName"],
}

# These element tags are ORDER-SENSITIVE in Salesforce (picklist display order,
# filter criteria evaluation order, layout field placement). Do NOT sort them;
# preserve original order.
_ORDERED_ELEMENT_TAGS: frozenset[str] = frozenset({
    "customValue",
    "standardValue",
    "value",
    "values",
    "filterItems",
    "summaryFilterItems",
    "layoutItems",
    # criteriaItems are positional: booleanFilter ("1 AND (2 OR 3)") indexes
    # them by number, so a reorder changes the evaluated expression.
    "criteriaItems",
})

# Tags that are order-sensitive only under a specific parent. The tag name
# alone is not proof of unordered semantics: "fields" under
# CustomObjectTranslation is a keyed set that must keep sorting, while the
# same tag under Flow <screens> is the rendered form order, and under Layout
# <relatedLists> it is the column order. Likewise "rules" is a keyed set in
# Workflow metadata but an ordered outcome list in Flow <decisions>
# (first match wins; see Salesforce Metadata API Developer Guide, FlowDecision).
_ORDERED_IN_PARENT: frozenset[tuple[str, str]] = frozenset({
    ("fields", "relatedLists"),
    # Flow: decision outcomes are evaluated in listed order.
    ("rules", "decisions"),
    # Flow: screen field order is the rendered form order.
    ("fields", "screens"),
    # Flow: the first satisfied wait event resumes the interview.
    ("waitEvents", "waits"),
    # Flow: scheduled paths run in listed order.
    ("scheduledPaths", "start"),
    # Flow: Get Records applies sort options in listed order.
    ("sortOptions", "recordLookups"),
})

# Maximum XML file size to normalize; larger files are returned as-is.
# This prevents memory exhaustion from very large Profiles / Permission Sets.
_MAX_XML_BYTES = 5 * 1024 * 1024  # 5 MB

# Elements whose text is a number; the API sometimes round-trips "18" as
# "18.0" (and vice versa). Only these curated elements get numeric
# canonicalisation — string-ish values elsewhere may legitimately carry
# leading zeros or trailing ".0".
_NUMERIC_ELEMENTS: frozenset[str] = frozenset({
    "precision", "scale", "length", "digits", "visibleLines",
    "relationshipOrder", "sortWeight", "rowLimit", "numberOfInstances",
})

_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")

# Elements whose leaf text is a JSON document (OmniStudio config blobs) —
# canonicalised so key-order/whitespace churn doesn't diff, and pretty-printed
# so real changes diff per-key. Curated: a generic element (e.g. a CustomLabel
# <value>) whose text merely LOOKS like JSON is user-visible verbatim, so its
# exact formatting is content and must not be rewritten.
_JSON_ELEMENTS: frozenset[str] = frozenset({
    "propertySetConfig", "propertySet", "elementTypeComponentMapping",
    "transformValuesMappings", "previewJsonData", "entityPayload",
    "dataSourceConfig", "stylingConfiguration", "sampleDataSourceResponse",
    "expectedInputJson", "expectedOutputJson",
})


def _canonical_number(text: str) -> str:
    """'18.0' → '18', '9.50' → '9.5', '18' → '18'. Assumes _NUMERIC_RE matched."""
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"

# Fallback priority when a tag is not in _ELEMENT_KEY_FIELDS
_FALLBACK_KEY_CHILDREN = ("fullName", "name")


# ---------------------------------------------------------------------------
# Sort-key extraction
# ---------------------------------------------------------------------------

def _child_text(el: ET.Element, local_name: str) -> str:
    """Return stripped text of a direct child with the given local tag name, or ''."""
    # Try with SF namespace first, then without
    for tag in (f"{_SF_NS_PREFIX}{local_name}", local_name):
        child = el.find(tag)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _element_sort_key(el: ET.Element) -> str:
    """Return a stable, case-insensitive sort key for a sibling element."""
    local = _local(el.tag)

    key_spec = _ELEMENT_KEY_FIELDS.get(local)
    if key_spec is None:
        # Fallback: try standard priority fields
        for field in _FALLBACK_KEY_CHILDREN:
            v = _child_text(el, field)
            if v:
                return v.lower()
        # Last resort: concatenate all immediate child text values
        return "".join((c.text or "").strip() for c in el).lower()

    if isinstance(key_spec, list):
        parts = [_child_text(el, f) for f in key_spec]
        return ".".join(p for p in parts if p).lower()

    return _child_text(el, key_spec).lower()


def _content_key(el: ET.Element) -> str:
    """Whitespace-insensitive canonical string of an element's full content.

    Used only to tie-break siblings whose identity keys are equal; must not
    depend on formatting (indentation/tails), which ET.tostring would include.
    """
    parts = [_local(el.tag), (el.text or "").strip()]
    parts.extend(f"{k}={v}" for k, v in sorted(el.attrib.items()))
    parts.extend(_content_key(c) for c in el)
    return "\x00".join(parts)


def _sorted_group(group: list[ET.Element]) -> list[ET.Element]:
    """Sort same-tag siblings by identity key; ties (identical keys) are
    refined with _content_key so the order is deterministic on both sides.
    The content key is only computed for tied runs — keys are unique in the
    common case (Profile permissions etc.) and full-content keys are not free.
    """
    decorated = sorted(((_element_sort_key(c), c) for c in group), key=lambda t: t[0])
    out: list[ET.Element] = []
    i = 0
    while i < len(decorated):
        j = i + 1
        while j < len(decorated) and decorated[j][0] == decorated[i][0]:
            j += 1
        run = [c for _, c in decorated[i:j]]
        if len(run) > 1:
            run.sort(key=_content_key)
        out.extend(run)
        i = j
    return out


# ---------------------------------------------------------------------------
# Recursive normalisation
# ---------------------------------------------------------------------------

# Leaf elements whose text is a case-insensitive API-name reference —
# retrieves round-trip these in varying case (observed: org returned
# adm_salesforcesystemadministrator where source had the uppercased form).
# A case-only difference there can never be a deployable change.
_CASE_INSENSITIVE_REFS: frozenset[str] = frozenset({"profile", "profiles"})


def _normalize_leaf_text(text: str | None) -> str | None:
    """Canonicalise a leaf element's text content.

    Strips insignificant leading/trailing whitespace (e.g. a trailing space an
    editor added to a <description>/<value> field) and canonicalises Unicode to
    NFC, so an org round-trip that re-encodes accented characters (NFC vs NFD)
    doesn't register as a content change. Internal whitespace is left untouched
    since it can be meaningful (e.g. multi-line formulas).
    """
    if text is None:
        return None
    normalized = unicodedata.normalize("NFC", text).strip()
    return normalized or None


def _normalize_embedded_json(text: str) -> str:
    """Canonicalise a leaf value that is itself a JSON document.

    OmniStudio metadata embeds JSON blobs in XML leaf text (propertySetConfig,
    elementTypeComponentMapping, previewJsonData, …) whose key order and
    whitespace shift between retrieves. Sorting keys and pretty-printing makes
    those blobs stable AND turns one-line blob diffs into readable per-key
    diffs. Object key order is not semantic in JSON; array order is preserved.
    Non-JSON text that merely starts with '{' or '[' (JS code, formulas) is
    returned unchanged.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, (dict, list)):
        return text
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)


def _normalize_element(el: ET.Element) -> None:
    """Normalise an element in-place: sort attributes, sort/recurse children."""
    # Sort XML attributes alphabetically
    if el.attrib:
        el.attrib = dict(sorted(el.attrib.items()))

    children = list(el)
    if not children:
        el.text = _normalize_leaf_text(el.text)
        if el.text and _local(el.tag) in _CASE_INSENSITIVE_REFS:
            el.text = el.text.casefold()
        if (el.text and _local(el.tag) in _NUMERIC_ELEMENTS
                and _NUMERIC_RE.match(el.text)):
            el.text = _canonical_number(el.text)
        elif (el.text and el.text[0] in "{["
                and _local(el.tag) in _JSON_ELEMENTS):
            el.text = _normalize_embedded_json(el.text)
        return

    # Recurse into all children first
    for child in children:
        _normalize_element(child)

    # Sort same-tag sibling groups IN PLACE (each occurrence keeps its
    # document slot; only the members permute among their own positions) only
    # when the tag has an explicit identity key in _ELEMENT_KEY_FIELDS for
    # this parent — order-insensitivity must be justified, never assumed.
    # Unkeyed repeating tags (layoutSections, Flow rule/wait/screen elements,
    # or an arbitrary homogeneous run) keep their document order, and slot
    # preservation means a contiguous-vs-interleaved difference is never
    # hidden. Equal keys are tie-broken by whitespace-insensitive content so
    # retrieves that shuffle identically keyed items (e.g.
    # omniDataTransformItem) still normalise identically.
    positions: dict[str, list[int]] = {}
    for i, c in enumerate(children):
        positions.setdefault(_local(c.tag), []).append(i)

    parent_tag = _local(el.tag)
    for t, idxs in positions.items():
        if (len(idxs) > 1 and t not in _ORDERED_ELEMENT_TAGS
                and (t, parent_tag) not in _ORDERED_IN_PARENT
                and t in _ELEMENT_KEY_FIELDS):
            group = _sorted_group([children[i] for i in idxs])
            for i, c in zip(idxs, group):
                children[i] = c

    # Re-attach children in the new order
    for child in list(el):
        el.remove(child)
    for child in children:
        el.append(child)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Retrieve-injected defaults (opt-in via baseline "strip_retrieve_defaults")
# ---------------------------------------------------------------------------
# The Metadata API emits optional elements with their default values on
# retrieve (e.g. <trackHistory>false</trackHistory>) that hand-authored
# source omits entirely — the single most common false-positive class.
# When enabled, a DIRECT CHILD of the root that equals its documented default
# is stripped from both sides before comparison. Curated and conservative:
# only booleans/enums whose Metadata API default is unambiguous. Nested
# occurrences (e.g. <fields> inside a legacy .object file) are NOT touched.
_RETRIEVE_DEFAULTS: dict[str, dict[str, str]] = {
    "CustomField": {
        "trackHistory": "false",
        "trackTrending": "false",
        "trackFeedHistory": "false",
        "externalId": "false",
        "required": "false",
        "unique": "false",
        "caseSensitive": "false",
        "deprecated": "false",
        "restrictedAdminField": "false",
        "writeRequiresMasterRead": "false",
        "reparentableMasterDetail": "false",
    },
    "CustomObject": {
        "enableFeeds": "false",
        "enableHistory": "false",
        "enableLicensing": "false",
        "deprecated": "false",
    },
    "ApexClass": {"status": "Active"},
    "ApexTrigger": {"status": "Active"},
    # OmniStudio: newer API versions inject this flag on retrieve; source
    # committed under an older version omits it entirely.
    "OmniScript": {"isManagedUsingStdDesigner": "false"},
    "OmniIntegrationProcedure": {"isManagedUsingStdDesigner": "false"},
}


def _strip_default_elements(root: ET.Element) -> None:
    table = _RETRIEVE_DEFAULTS.get(_local(root.tag))
    if table:
        for child in list(root):
            default = table.get(_local(child.tag))
            if default is not None and len(child) == 0:
                if (child.text or "").strip().lower() == default.lower():
                    root.remove(child)
    # *Settings documents are flat leaf maps: the API emits newer elements
    # with their default "false" while source committed under an older API
    # omits them, so a false leaf equals its absence. Deliberately not
    # applied to "true" — a present true vs absent could hide a real
    # enablement.
    if _local(root.tag).lower().endswith("settings"):
        for child in list(root):
            if len(child) == 0 and (child.text or "").strip().lower() == "false":
                root.remove(child)


# ---------------------------------------------------------------------------
# Profile/PermissionSet grant noise
# ---------------------------------------------------------------------------

# Roots whose direct children can be permission-grant elements.
_PROFILE_ROOTS: frozenset[str] = frozenset({
    "profile", "permissionset", "mutingpermissionset",
})

# Grant-bearing element tags (a subset of _ELEMENT_KEY_FIELDS keys that grant
# access). Elements without a verdict field (layoutAssignments, loginFlows,
# profileActionOverrides, …) are never dropped — the rule below requires at
# least one verdict leaf at its default value.
_GRANT_TAGS: frozenset[str] = frozenset({
    "applicationVisibilities",
    "categoryGroupVisibilities",
    "classAccesses",
    "customMetadataTypeAccesses",
    "customPermissions",
    "customSettingAccesses",
    "dataspaceScopes",
    "emailRoutingAddressAccesses",
    "externalCredentialPrincipalAccesses",
    "externalDataSourceAccesses",
    "fieldPermissions",
    "flowAccesses",
    "layoutAssignments",
    "loginFlows",
    "objectPermissions",
    "pageAccesses",
    "profileActionOverrides",
    "recordTypeVisibilities",
    "tabVisibilities",
    "userPermissions",
})

# Verdict leaf → platform default value (compared case-insensitively). A
# grant whose verdicts are all at default means "no access" — the same as
# the grant being absent, which is how the Metadata API represents it.
_VERDICT_DEFAULTS: dict[str, str] = {
    "enabled": "false",
    "visible": "false",
    "readable": "false",
    "editable": "false",
    "default": "false",
    "personaccountdefault": "false",
    "allowcreate": "false",
    "allowread": "false",
    "allowedit": "false",
    "allowdelete": "false",
    "modifyallrecords": "false",
    "viewallrecords": "false",
    "viewallfields": "false",
    "visibility": "defaulton",
}


# Suffixes that make ``Foo__c`` an unmanaged custom component, not the
# managed name ``foo__C``. A real namespace prefix is always followed by a
# further name (``ns__Thing__c``), never by just a suffix.
_CUSTOM_SUFFIXES: frozenset[str] = frozenset({
    "c", "e", "r", "x", "b", "del", "hd", "p", "mdt", "ka", "kav",
    "feed", "history", "share", "tag", "changeevent", "partner", "pov",
})


def managed_ns_of_name(name: str, managed: frozenset[str]) -> str | None:
    """The installed managed-package namespace that *name* (a single component
    name or one dot-separated reference segment) belongs to, e.g.
    ``LLC_BI__Collateral__c`` → ``llc_bi`` when ``llc_bi`` is in *managed*.

    Returns None for unmanaged names — including ``Foo__c``-style names where
    the part after ``__`` is only a standard custom suffix. *managed* is
    compared case-folded.
    """
    if "__" not in name:
        return None
    ns, rest = name.split("__", 1)
    if not ns or rest.casefold() in _CUSTOM_SUFFIXES:
        return None
    return ns.casefold() if ns.casefold() in managed else None


def _ref_has_managed_ns(ref: str, managed: frozenset[str]) -> bool:
    """True if any dot-separated segment of *ref* is ``<ns>__something`` with
    *ns* an installed managed-package namespace — covers both
    ``ns__Component`` refs and ``Object.ns__Field__c`` field refs."""
    return any(managed_ns_of_name(seg, managed) for seg in ref.split("."))


def _drop_profile_grant_noise(
    root: ET.Element, managed: frozenset[str] | None
) -> None:
    """Remove grant elements that cannot carry information:

    - the grant references an installed managed package's components
      (``ns__`` prefix) — a source manifest that does not track the package
      can never express them, and the org emits them for every profile;
    - every verdict leaf is at its platform default (enabled=false,
      visibility=DefaultOn, all-false objectPermissions, …) — semantically
      identical to the grant being absent.

    Any non-leaf child, missing verdict leaf, or leaf that is neither a
    verdict nor the element's reference key keeps the element.
    """
    managed_folded = frozenset(n.casefold() for n in managed) if managed else frozenset()
    for child in list(root):
        tag = _local(child.tag)
        if tag not in _GRANT_TAGS:
            continue
        key = _ELEMENT_KEY_FIELDS.get(tag)
        ref_fields = {key} if isinstance(key, str) else set(key or ())
        leaves = [c for c in child if len(c) == 0]
        if len(leaves) != len(child):  # nested structure — don't guess
            continue
        texts = {_local(c.tag): (c.text or "").strip() for c in leaves}
        if managed_folded and any(
            _ref_has_managed_ns(texts.get(f, ""), managed_folded) for f in ref_fields
        ):
            root.remove(child)
            continue
        verdicts = {
            t: v for t, v in texts.items() if t.lower() in _VERDICT_DEFAULTS
        }
        if (verdicts
                and all(v.lower() == _VERDICT_DEFAULTS[t.lower()] for t, v in verdicts.items())
                and all(t in ref_fields for t in texts if t.lower() not in _VERDICT_DEFAULTS)):
            root.remove(child)


def _strip_ignored_elements(el: ET.Element, ignore: frozenset[str]) -> None:
    """Remove any descendant element whose local tag name is in *ignore*.

    Used by baseline element rules (e.g. treat <apiVersion> churn as noise):
    both sides are stripped before comparison, so only the listed elements'
    differences are suppressed.
    """
    for child in list(el):
        if _local(child.tag) in ignore:
            el.remove(child)
        else:
            _strip_ignored_elements(child, ignore)


def normalize_xml(
    text: str,
    ignore_elements: frozenset[str] | None = None,
    strip_defaults: bool = False,
    managed_namespaces: frozenset[str] | None = None,
) -> str:
    """
    Parse *text* as XML, apply canonical normalisation, return canonical string.

    Returns the original *text* unchanged if it cannot be parsed as XML, so
    callers get a safe fallback for non-XML or malformed files.

    *ignore_elements*: local element names to drop entirely before
    normalisation (baseline noise rules). None/empty means keep everything.
    *strip_defaults*: drop root-level elements equal to their Metadata-API
    default value (see _RETRIEVE_DEFAULTS), and treat absent *Settings leaves
    as ``false``. Opt-in via the baseline.
    *managed_namespaces*: installed-package namespaces of the compared org;
    in Profile/PermissionSet documents, grant elements referencing
    ``ns__``-prefixed components are dropped (a source manifest cannot
    express them). Default-valued grants are always dropped under
    profile-type roots.
    """
    # Normalise line endings before parsing (avoids CRLF artefacts in text nodes)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if len(text.encode("utf-8")) > _MAX_XML_BYTES:
        return text  # Too large to normalize safely; caller treats files as different

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return text  # Not valid XML — return as-is

    if ignore_elements:
        _strip_ignored_elements(root, ignore_elements)
    if strip_defaults:
        _strip_default_elements(root)
    if _local(root.tag).lower() in _PROFILE_ROOTS:
        _drop_profile_grant_noise(root, managed_namespaces)

    _normalize_element(root)

    # Re-indent with consistent 4-space indentation (Python ≥ 3.9)
    ET.indent(root, space="    ")

    body = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


def normalize_xml_lines(
    path: Path, managed_namespaces: frozenset[str] | None = None
) -> list[str]:
    """
    Read *path*, normalise its XML content, and return lines with line endings.

    Falls back to raw lines (with CRLF→LF normalisation) if the file is not
    valid XML, so the caller can always use the result for diffing.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    normalised = normalize_xml(
        text, managed_namespaces=managed_namespaces
    )  # safe: returns original text on parse error
    return normalised.splitlines(keepends=True)


def xml_semantically_equal(
    lp: Path,
    rp: Path,
    ignore_elements: frozenset[str] | None = None,
    strip_defaults: bool = False,
    managed_namespaces: frozenset[str] | None = None,
) -> bool:
    """
    Return True if *lp* and *rp* represent the same XML after normalisation.

    Returns False on any I/O or parse error so the caller treats the files as
    different (safe fallback — never hides a real difference).
    """
    try:
        lt = lp.read_text(encoding="utf-8", errors="replace")
        rt = rp.read_text(encoding="utf-8", errors="replace")
        return (normalize_xml(lt, ignore_elements, strip_defaults, managed_namespaces)
                == normalize_xml(rt, ignore_elements, strip_defaults, managed_namespaces))
    except Exception:
        return False
