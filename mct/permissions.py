"""Strip no-grant entries from Profile / PermissionSet XML.

A Profile or PermissionSet retrieved from an org is full of entries that grant
NOTHING — `<fieldPermissions>` with both flags false, `<userPermissions>` with
enabled=false, hidden tabs, invisible record types. They confer no access, but
they break deployments whenever the target org lacks the referenced feature,
field, or license ("row size too large", "unknown user permission X",
"field Foo__c does not exist"). Removing them changes effective access in NO
way; it only removes deploy landmines. This is the same cleaning step tools
like Copado apply before validation.

Only files whose ROOT element is Profile or PermissionSet are touched; any
other XML passes through unchanged.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

_SF_NS = "http://soap.sforce.com/2006/04/metadata"
ET.register_namespace("", _SF_NS)

_APPLICABLE_ROOTS = frozenset({"Profile", "PermissionSet", "MutingPermissionSet"})

# Groups removed when EVERY listed boolean child is false/absent.
_ALL_FALSE_GROUPS: dict[str, tuple[str, ...]] = {
    "fieldPermissions": ("editable", "readable"),
    "objectPermissions": (
        "allowCreate", "allowDelete", "allowEdit",
        "allowRead", "modifyAllRecords", "viewAllRecords",
    ),
}

# Groups removed when their single enabling flag is false/absent.
_ENABLED_FLAG_GROUPS: dict[str, str] = {
    "userPermissions": "enabled",
    "classAccesses": "enabled",
    "pageAccesses": "enabled",
    "customPermissions": "enabled",
    "customMetadataTypeAccesses": "enabled",
    "customSettingAccesses": "enabled",
    "flowAccesses": "enabled",
    "externalDataSourceAccesses": "enabled",
    "emailRoutingAddressAccesses": "enabled",
}

# Identity child used in the removal report for each group.
_IDENTITY_FIELDS: dict[str, str] = {
    "fieldPermissions": "field",
    "objectPermissions": "object",
    "userPermissions": "name",
    "classAccesses": "apexClass",
    "pageAccesses": "apexPage",
    "customPermissions": "name",
    "customMetadataTypeAccesses": "name",
    "customSettingAccesses": "name",
    "flowAccesses": "flow",
    "externalDataSourceAccesses": "externalDataSource",
    "emailRoutingAddressAccesses": "name",
    "recordTypeVisibilities": "recordType",
    "applicationVisibilities": "application",
    "tabVisibilities": "tab",
    "tabSettings": "tab",
}


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _child_text(el: ET.Element, name: str) -> str:
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _is_false(el: ET.Element, name: str) -> bool:
    return _child_text(el, name).lower() in ("", "false")


def _grants_nothing(el: ET.Element) -> bool:
    group = _local(el.tag)

    fields = _ALL_FALSE_GROUPS.get(group)
    if fields is not None:
        return all(_is_false(el, f) for f in fields)

    flag = _ENABLED_FLAG_GROUPS.get(group)
    if flag is not None:
        return _is_false(el, flag)

    if group == "recordTypeVisibilities":
        # Invisible AND not any kind of default — pure no-grant.
        return (_is_false(el, "visible") and _is_false(el, "default")
                and _is_false(el, "personAccountDefault"))
    if group == "applicationVisibilities":
        return _is_false(el, "visible") and _is_false(el, "default")
    if group == "tabVisibilities":  # Profile: DefaultOn / DefaultOff / Hidden
        return _child_text(el, "visibility").lower() == "hidden"
    if group == "tabSettings":  # PermissionSet: Available / Visible / None
        return _child_text(el, "visibility").lower() == "none"
    return False


def strip_no_grant_permissions(xml_text: str) -> tuple[str, list[str]]:
    """Return (cleaned XML text, list of removed entries like
    'fieldPermissions:Account.Foo__c').

    Non-Profile/PermissionSet XML, or unparsable text, is returned unchanged
    with an empty removal list — safe to call on any file.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return xml_text, []
    if _local(root.tag) not in _APPLICABLE_ROOTS:
        return xml_text, []

    removed: list[str] = []
    for child in list(root):
        if _grants_nothing(child):
            group = _local(child.tag)
            ident = _child_text(child, _IDENTITY_FIELDS.get(group, "name")) or "?"
            removed.append(f"{group}:{ident}")
            root.remove(child)

    if not removed:
        return xml_text, []

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n", removed
