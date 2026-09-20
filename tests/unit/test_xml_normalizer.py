"""
Unit tests for scripts/xml_normalizer.py.

Covers: normalize_xml, normalize_xml_lines, xml_semantically_equal.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from xml_normalizer import normalize_xml, normalize_xml_lines, xml_semantically_equal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SF_NS = "http://soap.sforce.com/2006/04/metadata"


def _wrap(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<root xmlns="{SF_NS}">{body}</root>\n'


def _obj(fields_xml: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<CustomObject xmlns="{SF_NS}">\n'
        f'{fields_xml}'
        f'</CustomObject>\n'
    )


# ---------------------------------------------------------------------------
# normalize_xml — basic structural rules
# ---------------------------------------------------------------------------

class TestNormalizeXmlDeclaration:
    def test_always_emits_declaration(self):
        xml = f'<CustomObject xmlns="{SF_NS}"><label>X</label></CustomObject>'
        out = normalize_xml(xml)
        assert out.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')

    def test_trailing_newline(self):
        xml = f'<root xmlns="{SF_NS}"/>'
        out = normalize_xml(xml)
        assert out.endswith("\n")


class TestNormalizeXmlCRLF:
    def test_crlf_normalised(self):
        lf_xml = f'<root xmlns="{SF_NS}">\r\n<a>1</a>\r\n</root>'
        crlf_xml = f'<root xmlns="{SF_NS}">\n<a>1</a>\n</root>'
        assert normalize_xml(lf_xml) == normalize_xml(crlf_xml)

    def test_standalone_cr_normalised(self):
        cr_xml = f'<root xmlns="{SF_NS}">\r<a>1</a>\r</root>'
        lf_xml = f'<root xmlns="{SF_NS}">\n<a>1</a>\n</root>'
        assert normalize_xml(cr_xml) == normalize_xml(lf_xml)


class TestNormalizeXmlInvalidFallback:
    def test_invalid_xml_returns_original(self):
        bad = "not xml at all <<<"
        assert normalize_xml(bad) == bad

    def test_truncated_xml_returns_original(self):
        bad = f'<root xmlns="{SF_NS}"><open>'
        assert normalize_xml(bad) == bad


class TestNormalizeXmlAttributes:
    def test_attributes_sorted_alphabetically(self):
        xml_z_first = f'<root xmlns="{SF_NS}"><el z="2" a="1" m="3"/></root>'
        xml_a_first = f'<root xmlns="{SF_NS}"><el a="1" m="3" z="2"/></root>'
        assert normalize_xml(xml_z_first) == normalize_xml(xml_a_first)


class TestNormalizeXmlEmptyElements:
    def test_self_closing_and_expanded_equal(self):
        self_closing = f'<root xmlns="{SF_NS}"><a/></root>'
        expanded = f'<root xmlns="{SF_NS}"><a></a></root>'
        assert normalize_xml(self_closing) == normalize_xml(expanded)


class TestNormalizeXmlLeafText:
    """Leaf text content is trimmed and Unicode-canonicalised — reduces
    false positives from editor-added trailing whitespace or an org retrieve
    that re-encodes accented characters (NFC vs NFD)."""

    def test_trailing_whitespace_in_value_equal(self):
        a = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>Hello World</value></labels></CustomLabels>'
        b = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>Hello World  </value></labels></CustomLabels>'
        assert normalize_xml(a) == normalize_xml(b)

    def test_leading_and_trailing_whitespace_equal(self):
        a = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>Hello</value></labels></CustomLabels>'
        b = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>\n  Hello\n</value></labels></CustomLabels>'
        assert normalize_xml(a) == normalize_xml(b)

    def test_unicode_nfc_nfd_equal(self):
        nfd = "café"  # e + combining acute accent
        nfc = "café"  # precomposed e-acute
        a = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>{nfd}</value></labels></CustomLabels>'
        b = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>{nfc}</value></labels></CustomLabels>'
        assert normalize_xml(a) == normalize_xml(b)

    def test_internal_whitespace_preserved(self):
        """Internal whitespace (e.g. multi-line formulas) is not collapsed."""
        a = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>Hello   World</value></labels></CustomLabels>'
        b = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>Hello World</value></labels></CustomLabels>'
        assert normalize_xml(a) != normalize_xml(b)

    def test_genuine_text_difference_still_detected(self):
        a = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>Hello World</value></labels></CustomLabels>'
        b = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>Hello Mars</value></labels></CustomLabels>'
        assert normalize_xml(a) != normalize_xml(b)


class TestNumericNormalization:
    """Curated numeric elements canonicalise '18.0' vs '18' (roadmap 2.8)."""

    def test_precision_trailing_zero_equal(self):
        a = f'<CustomField xmlns="{SF_NS}"><fullName>X__c</fullName><precision>18</precision></CustomField>'
        b = f'<CustomField xmlns="{SF_NS}"><fullName>X__c</fullName><precision>18.0</precision></CustomField>'
        assert normalize_xml(a) == normalize_xml(b)

    def test_scale_decimal_preserved(self):
        a = f'<CustomField xmlns="{SF_NS}"><scale>9.5</scale></CustomField>'
        b = f'<CustomField xmlns="{SF_NS}"><scale>9.50</scale></CustomField>'
        c = f'<CustomField xmlns="{SF_NS}"><scale>9.05</scale></CustomField>'
        assert normalize_xml(a) == normalize_xml(b)
        assert normalize_xml(a) != normalize_xml(c)

    def test_genuine_numeric_difference_detected(self):
        a = f'<CustomField xmlns="{SF_NS}"><precision>18</precision></CustomField>'
        b = f'<CustomField xmlns="{SF_NS}"><precision>17</precision></CustomField>'
        assert normalize_xml(a) != normalize_xml(b)

    def test_non_numeric_elements_untouched(self):
        """Leading zeros elsewhere are meaningful strings, not numbers."""
        a = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>01</value></labels></CustomLabels>'
        b = f'<CustomLabels xmlns="{SF_NS}"><labels><fullName>X</fullName><value>1</value></labels></CustomLabels>'
        assert normalize_xml(a) != normalize_xml(b)


class TestStripRetrieveDefaults:
    """Opt-in registry of Metadata-API-injected default elements (roadmap 2.7)."""

    def test_default_element_stripped_when_enabled(self):
        with_default = (
            f'<CustomField xmlns="{SF_NS}"><fullName>X__c</fullName>'
            f'<trackHistory>false</trackHistory><type>Text</type></CustomField>'
        )
        without = f'<CustomField xmlns="{SF_NS}"><fullName>X__c</fullName><type>Text</type></CustomField>'
        assert normalize_xml(with_default, strip_defaults=True) == normalize_xml(without, strip_defaults=True)
        # off by default: still a real difference
        assert normalize_xml(with_default) != normalize_xml(without)

    def test_non_default_value_never_stripped(self):
        tracked = (
            f'<CustomField xmlns="{SF_NS}"><fullName>X__c</fullName>'
            f'<trackHistory>true</trackHistory></CustomField>'
        )
        untracked = f'<CustomField xmlns="{SF_NS}"><fullName>X__c</fullName></CustomField>'
        assert normalize_xml(tracked, strip_defaults=True) != normalize_xml(untracked, strip_defaults=True)

    def test_apex_class_status_active_default(self):
        a = f'<ApexClass xmlns="{SF_NS}"><apiVersion>59.0</apiVersion><status>Active</status></ApexClass>'
        b = f'<ApexClass xmlns="{SF_NS}"><apiVersion>59.0</apiVersion></ApexClass>'
        assert normalize_xml(a, strip_defaults=True) == normalize_xml(b, strip_defaults=True)

    def test_unknown_root_untouched(self):
        a = f'<Profile xmlns="{SF_NS}"><custom>false</custom></Profile>'
        b = f'<Profile xmlns="{SF_NS}"/>'
        assert normalize_xml(a, strip_defaults=True) != normalize_xml(b, strip_defaults=True)


# ---------------------------------------------------------------------------
# normalize_xml — element ordering
# ---------------------------------------------------------------------------

class TestNormalizeXmlElementOrdering:
    def test_homogeneous_siblings_sorted(self):
        """fieldPermissions sorted by <field> child."""
        xml_a = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<PermissionSet xmlns="{SF_NS}">\n'
            f'    <fieldPermissions><field>Account.Name</field><editable>true</editable></fieldPermissions>\n'
            f'    <fieldPermissions><field>Account.BillingCity</field><editable>false</editable></fieldPermissions>\n'
            f'</PermissionSet>\n'
        )
        xml_b = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<PermissionSet xmlns="{SF_NS}">\n'
            f'    <fieldPermissions><field>Account.BillingCity</field><editable>false</editable></fieldPermissions>\n'
            f'    <fieldPermissions><field>Account.Name</field><editable>true</editable></fieldPermissions>\n'
            f'</PermissionSet>\n'
        )
        assert normalize_xml(xml_a) == normalize_xml(xml_b)

    def test_object_permissions_sorted_by_object(self):
        xml_a = (
            f'<PermissionSet xmlns="{SF_NS}">'
            f'<objectPermissions><object>Contact</object></objectPermissions>'
            f'<objectPermissions><object>Account</object></objectPermissions>'
            f'</PermissionSet>'
        )
        xml_b = (
            f'<PermissionSet xmlns="{SF_NS}">'
            f'<objectPermissions><object>Account</object></objectPermissions>'
            f'<objectPermissions><object>Contact</object></objectPermissions>'
            f'</PermissionSet>'
        )
        assert normalize_xml(xml_a) == normalize_xml(xml_b)

    def test_heterogeneous_siblings_keep_order(self):
        """Mixed-tag siblings must NOT be reordered."""
        xml = (
            f'<CustomObject xmlns="{SF_NS}">'
            f'<fullName>Foo__c</fullName>'
            f'<label>Foo</label>'
            f'<pluralLabel>Foos</pluralLabel>'
            f'</CustomObject>'
        )
        out = normalize_xml(xml)
        full_pos = out.index("<fullName>")
        label_pos = out.index("<label>")
        plural_pos = out.index("<pluralLabel>")
        assert full_pos < label_pos < plural_pos

    def test_ordered_tag_customvalue_not_sorted(self):
        """customValue (picklist order) must preserve original sequence."""
        xml_a = (
            f'<GlobalValueSet xmlns="{SF_NS}">'
            f'<customValue><fullName>Zeta</fullName></customValue>'
            f'<customValue><fullName>Alpha</fullName></customValue>'
            f'</GlobalValueSet>'
        )
        xml_b = (
            f'<GlobalValueSet xmlns="{SF_NS}">'
            f'<customValue><fullName>Alpha</fullName></customValue>'
            f'<customValue><fullName>Zeta</fullName></customValue>'
            f'</GlobalValueSet>'
        )
        assert normalize_xml(xml_a) != normalize_xml(xml_b)

    def test_labels_sorted_by_fullname(self):
        xml_a = (
            f'<CustomLabels xmlns="{SF_NS}">'
            f'<labels><fullName>ZLabel</fullName><value>Z</value></labels>'
            f'<labels><fullName>ALabel</fullName><value>A</value></labels>'
            f'</CustomLabels>'
        )
        xml_b = (
            f'<CustomLabels xmlns="{SF_NS}">'
            f'<labels><fullName>ALabel</fullName><value>A</value></labels>'
            f'<labels><fullName>ZLabel</fullName><value>Z</value></labels>'
            f'</CustomLabels>'
        )
        assert normalize_xml(xml_a) == normalize_xml(xml_b)

    def test_composite_key_layout_assignments(self):
        xml_a = (
            f'<Profile xmlns="{SF_NS}">'
            f'<layoutAssignments><layout>Account-Z Layout</layout></layoutAssignments>'
            f'<layoutAssignments><layout>Account-A Layout</layout></layoutAssignments>'
            f'</Profile>'
        )
        xml_b = (
            f'<Profile xmlns="{SF_NS}">'
            f'<layoutAssignments><layout>Account-A Layout</layout></layoutAssignments>'
            f'<layoutAssignments><layout>Account-Z Layout</layout></layoutAssignments>'
            f'</Profile>'
        )
        assert normalize_xml(xml_a) == normalize_xml(xml_b)


# ---------------------------------------------------------------------------
# normalize_xml — no overcorrection (real differences still show)
# ---------------------------------------------------------------------------

class TestNormalizeXmlNoOvercorrection:
    def test_different_content_not_equal(self):
        xml_a = (
            f'<PermissionSet xmlns="{SF_NS}">'
            f'<fieldPermissions><field>Account.Name</field><editable>true</editable></fieldPermissions>'
            f'</PermissionSet>'
        )
        xml_b = (
            f'<PermissionSet xmlns="{SF_NS}">'
            f'<fieldPermissions><field>Account.Name</field><editable>false</editable></fieldPermissions>'
            f'</PermissionSet>'
        )
        assert normalize_xml(xml_a) != normalize_xml(xml_b)

    def test_added_element_detected(self):
        xml_a = (
            f'<PermissionSet xmlns="{SF_NS}">'
            f'<objectPermissions><object>Account</object></objectPermissions>'
            f'</PermissionSet>'
        )
        xml_b = (
            f'<PermissionSet xmlns="{SF_NS}">'
            f'<objectPermissions><object>Account</object></objectPermissions>'
            f'<objectPermissions><object>Contact</object></objectPermissions>'
            f'</PermissionSet>'
        )
        assert normalize_xml(xml_a) != normalize_xml(xml_b)

    def test_removed_element_detected(self):
        xml_full = (
            f'<CustomLabels xmlns="{SF_NS}">'
            f'<labels><fullName>A</fullName></labels>'
            f'<labels><fullName>B</fullName></labels>'
            f'</CustomLabels>'
        )
        xml_partial = (
            f'<CustomLabels xmlns="{SF_NS}">'
            f'<labels><fullName>A</fullName></labels>'
            f'</CustomLabels>'
        )
        assert normalize_xml(xml_full) != normalize_xml(xml_partial)


# ---------------------------------------------------------------------------
# normalize_xml — indentation normalisation
# ---------------------------------------------------------------------------

class TestNormalizeXmlIndentation:
    def test_different_indentation_equal(self):
        xml_2space = (
            f'<PermissionSet xmlns="{SF_NS}">\n'
            f'  <fieldPermissions>\n'
            f'    <field>Account.Name</field>\n'
            f'    <editable>true</editable>\n'
            f'  </fieldPermissions>\n'
            f'</PermissionSet>'
        )
        xml_4space = (
            f'<PermissionSet xmlns="{SF_NS}">\n'
            f'    <fieldPermissions>\n'
            f'        <field>Account.Name</field>\n'
            f'        <editable>true</editable>\n'
            f'    </fieldPermissions>\n'
            f'</PermissionSet>'
        )
        assert normalize_xml(xml_2space) == normalize_xml(xml_4space)

    def test_tab_vs_spaces_equal(self):
        xml_tabs = (
            f'<PermissionSet xmlns="{SF_NS}">\n'
            f'\t<fieldPermissions>\n'
            f'\t\t<field>Account.Name</field>\n'
            f'\t</fieldPermissions>\n'
            f'</PermissionSet>'
        )
        xml_spaces = (
            f'<PermissionSet xmlns="{SF_NS}">\n'
            f'    <fieldPermissions>\n'
            f'        <field>Account.Name</field>\n'
            f'    </fieldPermissions>\n'
            f'</PermissionSet>'
        )
        assert normalize_xml(xml_tabs) == normalize_xml(xml_spaces)


# ---------------------------------------------------------------------------
# normalize_xml_lines
# ---------------------------------------------------------------------------

class TestNormalizeXmlLines:
    def test_returns_list_of_strings(self, tmp_path):
        f = tmp_path / "test.xml"
        f.write_text(
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<root xmlns="{SF_NS}"><a>1</a></root>\n'
        )
        lines = normalize_xml_lines(f)
        assert isinstance(lines, list)
        assert all(isinstance(l, str) for l in lines)

    def test_lines_end_with_newline(self, tmp_path):
        f = tmp_path / "test.xml"
        f.write_text(f'<root xmlns="{SF_NS}"><a>1</a></root>')
        lines = normalize_xml_lines(f)
        for line in lines:
            assert line.endswith("\n"), f"Line missing newline: {line!r}"

    def test_invalid_xml_fallback_still_returns_lines(self, tmp_path):
        f = tmp_path / "bad.xml"
        f.write_text("not xml\nsecond line\n")
        lines = normalize_xml_lines(f)
        assert len(lines) >= 2


# ---------------------------------------------------------------------------
# xml_semantically_equal
# ---------------------------------------------------------------------------

class TestXmlSemanticallyEqual:
    def test_identical_files_equal(self, tmp_path):
        xml = f'<PermissionSet xmlns="{SF_NS}"><fieldPermissions><field>X</field></fieldPermissions></PermissionSet>'
        a = tmp_path / "a.xml"
        b = tmp_path / "b.xml"
        a.write_text(xml)
        b.write_text(xml)
        assert xml_semantically_equal(a, b)

    def test_reordered_elements_equal(self, tmp_path):
        a = tmp_path / "a.xml"
        b = tmp_path / "b.xml"
        a.write_text(
            f'<PermissionSet xmlns="{SF_NS}">'
            f'<fieldPermissions><field>B</field></fieldPermissions>'
            f'<fieldPermissions><field>A</field></fieldPermissions>'
            f'</PermissionSet>'
        )
        b.write_text(
            f'<PermissionSet xmlns="{SF_NS}">'
            f'<fieldPermissions><field>A</field></fieldPermissions>'
            f'<fieldPermissions><field>B</field></fieldPermissions>'
            f'</PermissionSet>'
        )
        assert xml_semantically_equal(a, b)

    def test_different_content_not_equal(self, tmp_path):
        a = tmp_path / "a.xml"
        b = tmp_path / "b.xml"
        a.write_text(f'<PermissionSet xmlns="{SF_NS}"><fieldPermissions><field>X</field><editable>true</editable></fieldPermissions></PermissionSet>')
        b.write_text(f'<PermissionSet xmlns="{SF_NS}"><fieldPermissions><field>X</field><editable>false</editable></fieldPermissions></PermissionSet>')
        assert not xml_semantically_equal(a, b)

    def test_missing_file_returns_false(self, tmp_path):
        a = tmp_path / "a.xml"
        b = tmp_path / "missing.xml"
        a.write_text(f'<root xmlns="{SF_NS}"/>')
        assert not xml_semantically_equal(a, b)

    def test_crlf_vs_lf_equal(self, tmp_path):
        a = tmp_path / "a.xml"
        b = tmp_path / "b.xml"
        a.write_bytes(
            f'<PermissionSet xmlns="{SF_NS}">\r\n<fieldPermissions><field>X</field></fieldPermissions>\r\n</PermissionSet>'.encode()
        )
        b.write_bytes(
            f'<PermissionSet xmlns="{SF_NS}">\n<fieldPermissions><field>X</field></fieldPermissions>\n</PermissionSet>'.encode()
        )
        assert xml_semantically_equal(a, b)

    def test_different_indentation_equal(self, tmp_path):
        a = tmp_path / "a.xml"
        b = tmp_path / "b.xml"
        a.write_text(f'<PermissionSet xmlns="{SF_NS}"><fieldPermissions><field>X</field></fieldPermissions></PermissionSet>')
        b.write_text(
            f'<PermissionSet xmlns="{SF_NS}">\n'
            f'    <fieldPermissions>\n'
            f'        <field>X</field>\n'
            f'    </fieldPermissions>\n'
            f'</PermissionSet>'
        )
        assert xml_semantically_equal(a, b)


# ---------------------------------------------------------------------------
# OmniStudio: keyed same-tag groups sorted inside heterogeneous parents
# ---------------------------------------------------------------------------

def _odt(items_xml: str) -> str:
    """OmniDataTransform root: scalar children + repeating omniDataTransformItem."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<OmniDataTransform xmlns="{SF_NS}">\n'
        f'    <name>DMCombine</name>\n'
        f'    <active>true</active>\n'
        f'{items_xml}'
        f'</OmniDataTransform>\n'
    )


class TestGroupScopedSorting:
    def test_omni_data_transform_items_sorted_within_heterogeneous_parent(self):
        """Repeating keyed items reordered between retrieves must normalize equal
        even though the parent also has scalar children (heterogeneous siblings)."""
        item_a = (
            '<omniDataTransformItem>'
            '<inputFieldName>A:Name</inputFieldName>'
            '<name>DMCombine</name>'
            '<outputFieldName>A:Name</outputFieldName>'
            '</omniDataTransformItem>'
        )
        item_b = (
            '<omniDataTransformItem>'
            '<inputFieldName>B:City</inputFieldName>'
            '<name>DMCombine</name>'
            '<outputFieldName>B:City</outputFieldName>'
            '</omniDataTransformItem>'
        )
        assert normalize_xml(_odt(item_a + item_b)) == normalize_xml(_odt(item_b + item_a))

    def test_scalar_group_positions_preserved(self):
        """Sorting within a keyed group must not move the scalar siblings around it."""
        item = (
            '<omniDataTransformItem>'
            '<inputFieldName>A:Name</inputFieldName>'
            '<name>DMCombine</name>'
            '</omniDataTransformItem>'
        )
        out = normalize_xml(_odt(item))
        assert out.index("<name>DMCombine</name>") < out.index("<active>") < out.index("<omniDataTransformItem>")

    def test_identical_key_tie_broken_by_content(self):
        """Items whose sort keys tie (same name, same fields) but whose other
        content differs must still normalize to a deterministic order."""
        item_x = (
            '<omniDataTransformItem>'
            '<inputFieldName>A:Name</inputFieldName>'
            '<name>DMCombine</name>'
            '<outputFieldName>A:Name</outputFieldName>'
            '<globalKey>aaa</globalKey>'
            '</omniDataTransformItem>'
        )
        item_y = (
            '<omniDataTransformItem>'
            '<inputFieldName>A:Name</inputFieldName>'
            '<name>DMCombine</name>'
            '<outputFieldName>A:Name</outputFieldName>'
            '<globalKey>bbb</globalKey>'
            '</omniDataTransformItem>'
        )
        assert normalize_xml(_odt(item_x + item_y)) == normalize_xml(_odt(item_y + item_x))

    def test_omni_process_elements_sorted_by_name(self):
        xml_a = (
            f'<OmniScript xmlns="{SF_NS}">'
            f'<name>Foo</name>'
            f'<omniProcessElements><name>Step2</name></omniProcessElements>'
            f'<omniProcessElements><name>Step1</name></omniProcessElements>'
            f'</OmniScript>'
        )
        xml_b = (
            f'<OmniScript xmlns="{SF_NS}">'
            f'<name>Foo</name>'
            f'<omniProcessElements><name>Step1</name></omniProcessElements>'
            f'<omniProcessElements><name>Step2</name></omniProcessElements>'
            f'</OmniScript>'
        )
        assert normalize_xml(xml_a) == normalize_xml(xml_b)

    def test_unkeyed_repeating_group_in_heterogeneous_parent_not_sorted(self):
        """Repeating tags with no entry in the key map (e.g. layout sections,
        where document order is meaningful) must keep their original order."""
        xml_a = (
            f'<Layout xmlns="{SF_NS}">'
            f'<fullName>Acct</fullName>'
            f'<layoutSections><label>Zeta</label></layoutSections>'
            f'<layoutSections><label>Alpha</label></layoutSections>'
            f'</Layout>'
        )
        xml_b = (
            f'<Layout xmlns="{SF_NS}">'
            f'<fullName>Acct</fullName>'
            f'<layoutSections><label>Alpha</label></layoutSections>'
            f'<layoutSections><label>Zeta</label></layoutSections>'
            f'</Layout>'
        )
        assert normalize_xml(xml_a) != normalize_xml(xml_b)

    def test_profile_keyed_groups_sorted_despite_scalar_siblings(self):
        """Profiles mix <custom> with repeating keyed groups; the keyed groups
        must now sort even though the sibling list is heterogeneous."""
        xml_a = (
            f'<Profile xmlns="{SF_NS}">'
            f'<custom>true</custom>'
            f'<classAccesses><apexClass>Zeta</apexClass><enabled>true</enabled></classAccesses>'
            f'<classAccesses><apexClass>Alpha</apexClass><enabled>true</enabled></classAccesses>'
            f'</Profile>'
        )
        xml_b = (
            f'<Profile xmlns="{SF_NS}">'
            f'<custom>true</custom>'
            f'<classAccesses><apexClass>Alpha</apexClass><enabled>true</enabled></classAccesses>'
            f'<classAccesses><apexClass>Zeta</apexClass><enabled>true</enabled></classAccesses>'
            f'</Profile>'
        )
        assert normalize_xml(xml_a) == normalize_xml(xml_b)


# ---------------------------------------------------------------------------
# Embedded JSON blobs in leaf text (OmniStudio propertySetConfig etc.)
# ---------------------------------------------------------------------------

class TestEmbeddedJsonNormalization:
    def test_key_order_shift_in_json_blob_equal(self):
        a = _wrap('<propertySetConfig>{"allowSaveForLater":true,"currencyCode":"NZD"}</propertySetConfig>')
        b = _wrap('<propertySetConfig>{"currencyCode":"NZD","allowSaveForLater":true}</propertySetConfig>')
        assert normalize_xml(a) == normalize_xml(b)

    def test_json_whitespace_shift_equal(self):
        a = _wrap('<propertySetConfig>{"a": 1, "b": [1, 2]}</propertySetConfig>')
        b = _wrap('<propertySetConfig>{"a":1,"b":[1,2]}</propertySetConfig>')
        assert normalize_xml(a) == normalize_xml(b)

    def test_json_array_order_is_preserved(self):
        """Array order is semantic in JSON — must NOT be normalized away."""
        a = _wrap('<propertySetConfig>{"steps":["one","two"]}</propertySetConfig>')
        b = _wrap('<propertySetConfig>{"steps":["two","one"]}</propertySetConfig>')
        assert normalize_xml(a) != normalize_xml(b)

    def test_non_json_braces_text_untouched(self):
        """JS code / formulas that merely start with '{' must be left alone."""
        code = '{ let x = 1; return x; }'
        out = normalize_xml(_wrap(f'<customJavaScript>{code}</customJavaScript>'))
        assert code in out

    def test_real_json_value_difference_still_detected(self):
        a = _wrap('<propertySetConfig>{"currencyCode":"NZD"}</propertySetConfig>')
        b = _wrap('<propertySetConfig>{"currencyCode":"AUD"}</propertySetConfig>')
        assert normalize_xml(a) != normalize_xml(b)


class TestOmniStudioRetrieveDefaults:
    def _os(self, extra: str = "") -> str:
        return (
            f'<OmniScript xmlns="{SF_NS}">'
            f'<name>Foo</name>'
            f'{extra}'
            f'</OmniScript>'
        )

    def test_is_managed_using_std_designer_default_stripped(self):
        """Newer API versions inject <isManagedUsingStdDesigner>false</> on
        retrieve; source committed under an older version omits it."""
        with_default = self._os('<isManagedUsingStdDesigner>false</isManagedUsingStdDesigner>')
        without = self._os()
        assert normalize_xml(with_default, strip_defaults=True) == normalize_xml(without, strip_defaults=True)

    def test_non_default_value_kept(self):
        with_true = self._os('<isManagedUsingStdDesigner>true</isManagedUsingStdDesigner>')
        without = self._os()
        assert normalize_xml(with_true, strip_defaults=True) != normalize_xml(without, strip_defaults=True)

    def test_integration_procedure_default_stripped(self):
        a = (
            f'<OmniIntegrationProcedure xmlns="{SF_NS}">'
            f'<name>IP</name>'
            f'<isManagedUsingStdDesigner>false</isManagedUsingStdDesigner>'
            f'</OmniIntegrationProcedure>'
        )
        b = f'<OmniIntegrationProcedure xmlns="{SF_NS}"><name>IP</name></OmniIntegrationProcedure>'
        assert normalize_xml(a, strip_defaults=True) == normalize_xml(b, strip_defaults=True)


class TestSortingPreservesDocumentSlots:
    def test_ordered_tag_interleaving_difference_not_hidden(self):
        """Non-contiguous occurrences of an order-sensitive tag must keep their
        document positions: contiguous vs interleaved layouts are a real diff."""
        xml_a = (
            f'<GlobalValueSet xmlns="{SF_NS}">'
            f'<customValue><fullName>V1</fullName></customValue>'
            f'<sorted>false</sorted>'
            f'<customValue><fullName>V0</fullName></customValue>'
            f'</GlobalValueSet>'
        )
        xml_b = (
            f'<GlobalValueSet xmlns="{SF_NS}">'
            f'<customValue><fullName>V1</fullName></customValue>'
            f'<customValue><fullName>V0</fullName></customValue>'
            f'<sorted>false</sorted>'
            f'</GlobalValueSet>'
        )
        assert normalize_xml(xml_a) != normalize_xml(xml_b)

    def test_tie_break_ignores_internal_formatting(self):
        """Identically-keyed items whose only difference is internal whitespace
        must sort the same way on both sides (formatting churn must not create
        a false diff via the tie-break)."""
        compact_x = ('<omniDataTransformItem><inputFieldName>A</inputFieldName>'
                     '<globalKey>k1</globalKey></omniDataTransformItem>')
        pretty_x = ('<omniDataTransformItem>\n    <inputFieldName>A</inputFieldName>\n'
                    '    <globalKey>k1</globalKey>\n</omniDataTransformItem>')
        compact_y = ('<omniDataTransformItem><inputFieldName>A</inputFieldName>'
                     '<globalKey>k2</globalKey></omniDataTransformItem>')
        pretty_y = ('<omniDataTransformItem>\n    <inputFieldName>A</inputFieldName>\n'
                    '    <globalKey>k2</globalKey>\n</omniDataTransformItem>')
        # Same two logical items on both sides; each item is formatted
        # differently per side, so a whitespace-sensitive tie-break would
        # order them differently and manufacture a diff.
        left = _odt(compact_x + pretty_y)
        right = _odt(pretty_x + compact_y)
        assert normalize_xml(left) == normalize_xml(right)


class TestEmbeddedJsonCuratedElements:
    def test_generic_element_json_lookalike_untouched(self):
        """A user-visible string that merely parses as JSON (e.g. a label
        <value>) must NOT be canonicalized — its exact text is the content."""
        a = _wrap('<value>{"amount": 1}</value>')
        b = _wrap('<value>{"amount":1}</value>')
        assert normalize_xml(a) != normalize_xml(b)


class TestLayoutOrderPreservation:
    """Layout field order and related-list column order are SEMANTIC — the
    normalizer must never make two different orderings compare equal."""

    @staticmethod
    def _layout(body: str) -> str:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Layout xmlns="{SF_NS}">{body}</Layout>\n'
        )

    def test_layout_items_order_is_preserved(self):
        items_ab = (
            '<layoutSections><layoutColumns>'
            '<layoutItems><behavior>Edit</behavior><field>Amount</field></layoutItems>'
            '<layoutItems><behavior>Edit</behavior><field>Name</field></layoutItems>'
            '</layoutColumns></layoutSections>'
        )
        items_ba = (
            '<layoutSections><layoutColumns>'
            '<layoutItems><behavior>Edit</behavior><field>Name</field></layoutItems>'
            '<layoutItems><behavior>Edit</behavior><field>Amount</field></layoutItems>'
            '</layoutColumns></layoutSections>'
        )
        assert normalize_xml(self._layout(items_ab)) != normalize_xml(self._layout(items_ba))

    def test_related_list_fields_order_is_preserved(self):
        rl_ab = (
            '<relatedLists><fields>NAME</fields><fields>STATUS</fields>'
            '<relatedList>RelatedContactList</relatedList></relatedLists>'
        )
        rl_ba = (
            '<relatedLists><fields>STATUS</fields><fields>NAME</fields>'
            '<relatedList>RelatedContactList</relatedList></relatedLists>'
        )
        assert normalize_xml(self._layout(rl_ab)) != normalize_xml(self._layout(rl_ba))

    def test_translation_fields_still_sorted_by_name(self):
        """CustomObjectTranslation <fields> groups are keyed by <name> and
        retrieve-order churn there must still normalize away."""
        f_a = '<fields><name>Alpha__c</name><label>Alpha</label></fields>'
        f_b = '<fields><name>Beta__c</name><label>Beta</label></fields>'
        cot = '<?xml version="1.0" encoding="UTF-8"?>\n<CustomObjectTranslation xmlns="%s">%%s</CustomObjectTranslation>\n' % SF_NS
        assert normalize_xml(cot % (f_a + f_b)) == normalize_xml(cot % (f_b + f_a))


# ---------------------------------------------------------------------------
# Case-insensitive API-name references
# ---------------------------------------------------------------------------

class TestCaseInsensitiveProfileRefs:
    """Salesforce API names are case-insensitive, but retrieves sometimes
    round-trip a reference in different case — observed live: org returned
    ``adm_salesforcesystemadministrator`` where source had
    ``ADM_SALESFORCESYSTEMADMINISTRATOR`` in ProfilePasswordPolicy, and a
    lowercased profile name in a SharingSet. A case-only difference in an
    API-name reference is never a deployable change."""

    def _ppp(self, name: str) -> str:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<ProfilePasswordPolicy xmlns="{SF_NS}">'
            f'<forgotPasswordRedirect>false</forgotPasswordRedirect>'
            f'<profile>{name}</profile>'
            f'</ProfilePasswordPolicy>\n'
        )

    def test_profile_reference_casefolded(self):
        a = self._ppp("ADM_SALESFORCESYSTEMADMINISTRATOR")
        b = self._ppp("adm_salesforcesystemadministrator")
        assert normalize_xml(a) == normalize_xml(b)

    def test_sharing_set_profiles_casefolded(self):
        def ss(name: str) -> str:
            return (
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<SharingSet xmlns="{SF_NS}">'
                f'<name>Trial Portal Users</name>'
                f'<profiles>{name}</profiles>'
                f'</SharingSet>\n'
            )
        assert normalize_xml(ss("Trial Customer Portal User")) == \
            normalize_xml(ss("trial customer portal user"))

    def test_other_text_stays_case_sensitive(self):
        a = _obj('<label>Foo</label>')
        b = _obj('<label>foo</label>')
        assert normalize_xml(a) != normalize_xml(b)


# ---------------------------------------------------------------------------
# Profile/PermissionSet grant noise
# ---------------------------------------------------------------------------

class TestProfileGrantDefaults:
    """A grant element whose verdict fields are all at platform default
    (enabled=false, visibility=DefaultOn, all-false objectPermissions, …)
    carries the same meaning as its absence — the Metadata API omits
    ungranted entries while committed source often records them. Observed
    live: org profiles emitted ~410 ``standard-*`` tabVisibilities the
    branch lacked purely because the org serialises defaults.

    Restricted to Profile/PermissionSet/MutingPermissionSet roots; any
    child leaf that is neither a known verdict field nor the element's
    reference key keeps the element (unknown structure = keep)."""

    def _profile(self, body: str, root: str = "Profile") -> str:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<{root} xmlns="{SF_NS}"><custom>false</custom>{body}</{root}>\n'
        )

    def test_default_on_tab_dropped(self):
        a = self._profile(
            '<tabVisibilities><tab>standard-AssessmentTaskOrder</tab>'
            '<visibility>DefaultOn</visibility></tabVisibilities>')
        b = self._profile('')
        assert normalize_xml(a) == normalize_xml(b)

    def test_hidden_tab_kept(self):
        a = self._profile(
            '<tabVisibilities><tab>standard-X</tab>'
            '<visibility>Hidden</visibility></tabVisibilities>')
        b = self._profile('')
        assert normalize_xml(a) != normalize_xml(b)

    def test_disabled_accesses_dropped(self):
        for tag, ref in (("classAccesses", "apexClass"),
                         ("pageAccesses", "apexPage"),
                         ("flowAccesses", "flow"),
                         ("customPermissions", "name"),
                         ("userPermissions", "name")):
            a = self._profile(
                f'<{tag}><enabled>false</enabled><{ref}>X</{ref}></{tag}>')
            assert normalize_xml(a) == normalize_xml(self._profile('')), tag

    def test_enabled_access_kept(self):
        a = self._profile(
            '<classAccesses><apexClass>X</apexClass><enabled>true</enabled></classAccesses>')
        assert normalize_xml(a) != normalize_xml(self._profile(''))

    def test_all_false_field_permissions_dropped(self):
        a = self._profile(
            '<fieldPermissions><editable>false</editable>'
            '<field>Account.F__c</field><readable>false</readable></fieldPermissions>')
        assert normalize_xml(a) == normalize_xml(self._profile(''))

    def test_partial_field_permissions_kept(self):
        a = self._profile(
            '<fieldPermissions><editable>false</editable>'
            '<field>Account.F__c</field><readable>true</readable></fieldPermissions>')
        assert normalize_xml(a) != normalize_xml(self._profile(''))

    def test_all_false_object_permissions_dropped(self):
        a = self._profile(
            '<objectPermissions><allowCreate>false</allowCreate>'
            '<allowDelete>false</allowDelete><allowEdit>false</allowEdit>'
            '<allowRead>false</allowRead><modifyAllRecords>false</modifyAllRecords>'
            '<object>Case</object><viewAllRecords>false</viewAllRecords>'
            '</objectPermissions>')
        assert normalize_xml(a) == normalize_xml(self._profile(''))

    def test_record_type_visibility_all_default_dropped(self):
        a = self._profile(
            '<recordTypeVisibilities><default>false</default>'
            '<recordType>Case.RT</recordType><visible>false</visible>'
            '</recordTypeVisibilities>')
        assert normalize_xml(a) == normalize_xml(self._profile(''))

    def test_default_record_type_kept(self):
        a = self._profile(
            '<recordTypeVisibilities><default>true</default>'
            '<recordType>Case.RT</recordType><visible>true</visible>'
            '</recordTypeVisibilities>')
        assert normalize_xml(a) != normalize_xml(self._profile(''))

    def test_no_verdict_leaf_never_dropped(self):
        # layoutAssignments has only reference fields — nothing marks it as
        # a default grant, so it must never be dropped.
        a = self._profile(
            '<layoutAssignments><layout>Case-Layout</layout></layoutAssignments>')
        assert normalize_xml(a) != normalize_xml(self._profile(''))

    def test_unknown_extra_leaf_keeps_element(self):
        a = self._profile(
            '<classAccesses><apexClass>X</apexClass><enabled>false</enabled>'
            '<somethingElse>y</somethingElse></classAccesses>')
        assert normalize_xml(a) != normalize_xml(self._profile(''))

    def test_permission_set_root_also_normalised(self):
        a = self._profile(
            '<tabVisibilities><tab>standard-X</tab>'
            '<visibility>DefaultOn</visibility></tabVisibilities>',
            root="PermissionSet")
        b = self._profile('', root="PermissionSet")
        assert normalize_xml(a) == normalize_xml(b)

    def test_non_profile_root_untouched(self):
        # <enabled>false</enabled> outside a profile root is content.
        a = f'<?xml version="1.0" encoding="UTF-8"?>\n<Flow xmlns="{SF_NS}">' \
            '<processMetadataValues><name>BuilderType</name>' \
            '<value><stringValue>false</stringValue></value>' \
            '</processMetadataValues></Flow>\n'
        b = f'<?xml version="1.0" encoding="UTF-8"?>\n<Flow xmlns="{SF_NS}"/>\n'
        assert normalize_xml(a) != normalize_xml(b)


class TestManagedNamespaceGrants:
    """Grant elements referencing installed managed-package components
    (``NS__``-prefixed references) cannot be expressed in a source manifest
    that does not track the package — both sides drop them when the org's
    managed namespaces are known. Observed live: ~620 LLC_BI__* tab
    visibilities surfaced as profile drift."""

    def _profile(self, body: str) -> str:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<Profile xmlns="{SF_NS}">{body}</Profile>\n'
        )

    def test_managed_tab_dropped_even_when_non_default(self):
        a = self._profile(
            '<tabVisibilities><tab>LLC_BI__Adverse_Action__c</tab>'
            '<visibility>Hidden</visibility></tabVisibilities>')
        assert normalize_xml(a, managed_namespaces=frozenset({"llc_bi"})) == \
            normalize_xml(self._profile(''), managed_namespaces=frozenset({"llc_bi"}))

    def test_managed_tab_kept_without_namespace_info(self):
        a = self._profile(
            '<tabVisibilities><tab>LLC_BI__Adverse_Action__c</tab>'
            '<visibility>Hidden</visibility></tabVisibilities>')
        assert normalize_xml(a) != normalize_xml(self._profile(''))

    def test_managed_field_permission_dropped(self):
        a = self._profile(
            '<fieldPermissions><editable>true</editable>'
            '<field>Opportunity.LLC_BI__Amount__c</field>'
            '<readable>true</readable></fieldPermissions>')
        assert normalize_xml(a, managed_namespaces=frozenset({"LLC_BI"})) == \
            normalize_xml(self._profile(''), managed_namespaces=frozenset({"LLC_BI"}))

    def test_unmanaged_field_permission_kept(self):
        a = self._profile(
            '<fieldPermissions><editable>true</editable>'
            '<field>Opportunity.Amount</field><readable>true</readable>'
            '</fieldPermissions>')
        assert normalize_xml(a, managed_namespaces=frozenset({"LLC_BI"})) != \
            normalize_xml(self._profile(''), managed_namespaces=frozenset({"LLC_BI"}))

    def test_custom_suffix_not_treated_as_namespace(self):
        # Foo__c is a custom-object suffix, not a namespace — must not be
        # dropped even if a namespace named "foo" is installed.
        a = self._profile(
            '<tabVisibilities><tab>Foo__c</tab><visibility>Hidden</visibility>'
            '</tabVisibilities>')
        assert normalize_xml(a, managed_namespaces=frozenset({"foo"})) != \
            normalize_xml(self._profile(''), managed_namespaces=frozenset({"foo"}))


# ---------------------------------------------------------------------------
# Settings: absent ≡ false under strip_defaults
# ---------------------------------------------------------------------------

class TestSettingsAbsentIsFalse:
    """*Settings documents are flat leaf maps; the Metadata API emits newer
    elements with their default ``false`` while older-API source omits them.
    Under strip_retrieve_defaults, a ``false`` leaf equals its absence.
    Deliberately NOT applied to ``true``: a present ``true`` vs absent may
    hide a real enablement, so it still diffs. Opt-in because a non-default
    ``false`` (element whose platform default is true) would be hidden."""

    def _settings(self, body: str, root: str = "CaseSettings") -> str:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<{root} xmlns="{SF_NS}">{body}</{root}>\n'
        )

    def test_false_leaf_equals_absent(self):
        a = self._settings('<enableEmailSenderIdCompliance>false</enableEmailSenderIdCompliance>')
        b = self._settings('')
        assert normalize_xml(a, strip_defaults=True) == normalize_xml(b, strip_defaults=True)

    def test_false_leaf_still_differs_without_opt_in(self):
        a = self._settings('<enableX>false</enableX>')
        b = self._settings('')
        assert normalize_xml(a) != normalize_xml(b)

    def test_true_leaf_still_differs(self):
        a = self._settings('<enableX>true</enableX>')
        b = self._settings('')
        assert normalize_xml(a, strip_defaults=True) != normalize_xml(b, strip_defaults=True)

    def test_non_boolean_leaf_still_differs(self):
        a = self._settings('<aiAttributionTimeframe>6</aiAttributionTimeframe>')
        b = self._settings('')
        assert normalize_xml(a, strip_defaults=True) != normalize_xml(b, strip_defaults=True)

    def test_only_settings_roots_affected(self):
        a = _obj('<enableFeeds>false</enableFeeds>')
        b = _obj('')
        # CustomObject.enableFeeds is already in _RETRIEVE_DEFAULTS; use a
        # leaf that is NOT curated to prove the Settings rule doesn't leak.
        c = _obj('<someUncuratedFlag>false</someUncuratedFlag>')
        assert normalize_xml(c, strip_defaults=True) != normalize_xml(b, strip_defaults=True)
        assert normalize_xml(a, strip_defaults=True) == normalize_xml(b, strip_defaults=True)
