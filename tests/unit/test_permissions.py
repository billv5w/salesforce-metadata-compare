"""Unit tests for mct/permissions.py — no-grant permission stripping."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mct.permissions import strip_no_grant_permissions

SF_NS = "http://soap.sforce.com/2006/04/metadata"


def _profile(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<Profile xmlns="{SF_NS}">{body}</Profile>'


def _permset(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<PermissionSet xmlns="{SF_NS}">{body}</PermissionSet>'


class TestFieldPermissions:
    def test_both_false_removed(self):
        xml = _profile(
            "<fieldPermissions><editable>false</editable>"
            "<field>Account.Legacy__c</field><readable>false</readable></fieldPermissions>"
        )
        out, removed = strip_no_grant_permissions(xml)
        assert removed == ["fieldPermissions:Account.Legacy__c"]
        assert "Legacy__c" not in out

    def test_readable_true_kept(self):
        xml = _profile(
            "<fieldPermissions><editable>false</editable>"
            "<field>Account.Kept__c</field><readable>true</readable></fieldPermissions>"
        )
        out, removed = strip_no_grant_permissions(xml)
        assert removed == []
        assert "Kept__c" in out


class TestObjectPermissions:
    def test_all_false_removed(self):
        flags = "".join(
            f"<{f}>false</{f}>" for f in
            ("allowCreate", "allowDelete", "allowEdit", "allowRead",
             "modifyAllRecords", "viewAllRecords")
        )
        xml = _profile(f"<objectPermissions>{flags}<object>Legacy__c</object></objectPermissions>")
        out, removed = strip_no_grant_permissions(xml)
        assert removed == ["objectPermissions:Legacy__c"]

    def test_read_true_kept(self):
        xml = _profile(
            "<objectPermissions><allowRead>true</allowRead>"
            "<object>Kept__c</object></objectPermissions>"
        )
        _, removed = strip_no_grant_permissions(xml)
        assert removed == []


class TestEnabledFlagGroups:
    def test_disabled_user_permission_removed(self):
        xml = _profile(
            "<userPermissions><enabled>false</enabled><name>ManageUsers</name></userPermissions>"
        )
        _, removed = strip_no_grant_permissions(xml)
        assert removed == ["userPermissions:ManageUsers"]

    def test_enabled_class_access_kept_disabled_removed(self):
        xml = _permset(
            "<classAccesses><apexClass>Kept</apexClass><enabled>true</enabled></classAccesses>"
            "<classAccesses><apexClass>Gone</apexClass><enabled>false</enabled></classAccesses>"
        )
        out, removed = strip_no_grant_permissions(xml)
        assert removed == ["classAccesses:Gone"]
        assert "Kept" in out and "Gone" not in out


class TestVisibilities:
    def test_invisible_non_default_record_type_removed(self):
        xml = _profile(
            "<recordTypeVisibilities><default>false</default>"
            "<recordType>Account.Legacy</recordType><visible>false</visible></recordTypeVisibilities>"
        )
        _, removed = strip_no_grant_permissions(xml)
        assert removed == ["recordTypeVisibilities:Account.Legacy"]

    def test_default_record_type_kept_even_if_invisible(self):
        xml = _profile(
            "<recordTypeVisibilities><default>true</default>"
            "<recordType>Account.Main</recordType><visible>false</visible></recordTypeVisibilities>"
        )
        _, removed = strip_no_grant_permissions(xml)
        assert removed == []

    def test_hidden_tab_removed_defaulton_kept(self):
        xml = _profile(
            "<tabVisibilities><tab>Legacy__c</tab><visibility>Hidden</visibility></tabVisibilities>"
            "<tabVisibilities><tab>Kept__c</tab><visibility>DefaultOn</visibility></tabVisibilities>"
        )
        out, removed = strip_no_grant_permissions(xml)
        assert removed == ["tabVisibilities:Legacy__c"]
        assert "Kept__c" in out

    def test_permset_tab_none_removed(self):
        xml = _permset(
            "<tabSettings><tab>Legacy__c</tab><visibility>None</visibility></tabSettings>"
        )
        _, removed = strip_no_grant_permissions(xml)
        assert removed == ["tabSettings:Legacy__c"]

    def test_invisible_app_removed(self):
        xml = _profile(
            "<applicationVisibilities><application>Old</application>"
            "<default>false</default><visible>false</visible></applicationVisibilities>"
        )
        _, removed = strip_no_grant_permissions(xml)
        assert removed == ["applicationVisibilities:Old"]


class TestSafety:
    def test_non_profile_untouched(self):
        xml = (
            f'<CustomObject xmlns="{SF_NS}">'
            "<fieldPermissions><editable>false</editable><readable>false</readable></fieldPermissions>"
            "</CustomObject>"
        )
        out, removed = strip_no_grant_permissions(xml)
        assert out == xml
        assert removed == []

    def test_unparsable_returned_unchanged(self):
        out, removed = strip_no_grant_permissions("not xml <<<")
        assert out == "not xml <<<"
        assert removed == []

    def test_no_removals_returns_original_text(self):
        xml = _profile("<userPermissions><enabled>true</enabled><name>X</name></userPermissions>")
        out, removed = strip_no_grant_permissions(xml)
        assert out == xml
        assert removed == []

    def test_output_still_valid_xml_with_ns(self):
        xml = _profile(
            "<userPermissions><enabled>false</enabled><name>Gone</name></userPermissions>"
            "<userPermissions><enabled>true</enabled><name>Kept</name></userPermissions>"
        )
        out, removed = strip_no_grant_permissions(xml)
        assert removed
        import xml.etree.ElementTree as ET
        root = ET.fromstring(out)
        assert root.tag == f"{{{SF_NS}}}Profile"
