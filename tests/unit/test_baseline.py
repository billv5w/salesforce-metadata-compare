"""Unit tests for mct/baseline.py and XML element-ignore rules."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from mct.baseline import (
    accept_diff,
    classify_entry,
    compute_fingerprint,
    default_baseline,
    ignore_reason,
    load_baseline,
    save_baseline,
    unaccept_diff,
    xml_ignore_elements,
)
from retrieved_folder_compare import compare_trees
from xml_normalizer import normalize_xml

SF_NS = "http://soap.sforce.com/2006/04/metadata"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadSave:
    def test_missing_file_yields_default(self, tmp_path):
        assert load_baseline(tmp_path / "nope.json") == default_baseline()

    def test_malformed_file_yields_default(self, tmp_path):
        p = _write(tmp_path / "baseline.json", "{not json")
        assert load_baseline(p) == default_baseline()

    def test_roundtrip(self, tmp_path):
        p = tmp_path / "baseline.json"
        data = default_baseline()
        data["ignore"]["types"] = ["reports"]
        data["ignore"]["paths"] = ["objects/Legacy*"]
        data["ignore"]["xml_elements"] = ["apiVersion"]
        data["accepted"]["classes/A.cls"] = {"fingerprint": "abc", "accepted_at": "x", "note": ""}
        save_baseline(data, p)
        assert load_baseline(p) == data

    def test_entries_without_fingerprint_dropped(self, tmp_path):
        p = _write(tmp_path / "b.json", '{"accepted": {"x": {"note": "no fp"}}}')
        assert load_baseline(p)["accepted"] == {}


class TestIgnoreReason:
    def _bl(self, **ignore):
        b = default_baseline()
        b["ignore"].update(ignore)
        return b

    def test_type_match_case_insensitive(self):
        b = self._bl(types=["Reports"])
        assert ignore_reason("reports/Sales/Pipeline.report-meta.xml", b) == "type:Reports"

    def test_path_glob_match(self):
        b = self._bl(paths=["objects/Legacy_*"])
        assert ignore_reason("objects/Legacy_Old__c/fields/X.field-meta.xml", b) == "path:objects/Legacy_*"

    def test_no_match_returns_none(self):
        b = self._bl(types=["reports"], paths=["objects/Legacy*"])
        assert ignore_reason("classes/Foo.cls", b) is None


class TestFingerprint:
    def test_stable_across_formatting_churn(self, tmp_path):
        a1 = _write(tmp_path / "a1.xml", f'<CustomObject xmlns="{SF_NS}"><label>X</label></CustomObject>')
        a2 = _write(tmp_path / "a2.xml", f'<CustomObject xmlns="{SF_NS}">\n    <label>X</label>\n</CustomObject>\n')
        b = _write(tmp_path / "b.xml", f'<CustomObject xmlns="{SF_NS}"><label>Y</label></CustomObject>')
        assert compute_fingerprint(a1, b) == compute_fingerprint(a2, b)

    def test_changes_when_content_changes(self, tmp_path):
        a = _write(tmp_path / "a.xml", f'<CustomObject xmlns="{SF_NS}"><label>X</label></CustomObject>')
        b1 = _write(tmp_path / "b1.xml", f'<CustomObject xmlns="{SF_NS}"><label>Y</label></CustomObject>')
        b2 = _write(tmp_path / "b2.xml", f'<CustomObject xmlns="{SF_NS}"><label>Z</label></CustomObject>')
        assert compute_fingerprint(a, b1) != compute_fingerprint(a, b2)

    def test_single_side_supported(self, tmp_path):
        a = _write(tmp_path / "a.cls", "public class A {}\n")
        fp_only_left = compute_fingerprint(a, None)
        fp_only_right = compute_fingerprint(None, a)
        assert fp_only_left != fp_only_right  # side matters
        assert fp_only_left == compute_fingerprint(a, None)  # stable


class TestClassifyEntry:
    def test_active_when_no_rules(self, tmp_path):
        a = _write(tmp_path / "a.cls", "v1")
        b = _write(tmp_path / "b.cls", "v2")
        status, _ = classify_entry("classes/A.cls", a, b, default_baseline())
        assert status == "active"

    def test_ignored_by_type(self, tmp_path):
        b = default_baseline()
        b["ignore"]["types"] = ["classes"]
        status, detail = classify_entry("classes/A.cls", None, None, b)
        assert status == "ignored"
        assert detail == "type:classes"

    def test_accepted_then_stale_on_change(self, tmp_path):
        bl_file = tmp_path / "baseline.json"
        a = _write(tmp_path / "a.cls", "v1")
        b = _write(tmp_path / "b.cls", "v2")
        accept_diff("classes/A.cls", a, b, note="known sandbox delta", baseline_file=bl_file)

        bl = load_baseline(bl_file)
        status, _ = classify_entry("classes/A.cls", a, b, bl)
        assert status == "accepted"

        b.write_text("v3", encoding="utf-8")  # drift moved — must resurface
        status, _ = classify_entry("classes/A.cls", a, b, bl)
        assert status == "accepted_stale"

    def test_unaccept(self, tmp_path):
        bl_file = tmp_path / "baseline.json"
        a = _write(tmp_path / "a.cls", "v1")
        accept_diff("classes/A.cls", a, None, baseline_file=bl_file)
        assert unaccept_diff("classes/A.cls", baseline_file=bl_file) is True
        assert unaccept_diff("classes/A.cls", baseline_file=bl_file) is False
        status, _ = classify_entry("classes/A.cls", a, None, load_baseline(bl_file))
        assert status == "active"


class TestXmlElementIgnores:
    def test_ignored_element_difference_is_equal(self):
        a = f'<ApexClass xmlns="{SF_NS}"><apiVersion>59.0</apiVersion><status>Active</status></ApexClass>'
        b = f'<ApexClass xmlns="{SF_NS}"><apiVersion>61.0</apiVersion><status>Active</status></ApexClass>'
        ig = frozenset({"apiVersion"})
        assert normalize_xml(a, ig) == normalize_xml(b, ig)
        assert normalize_xml(a) != normalize_xml(b)  # without the rule it differs

    def test_nested_ignored_element_stripped(self):
        a = f'<Flow xmlns="{SF_NS}"><metadata><apiVersion>59.0</apiVersion></metadata><label>F</label></Flow>'
        b = f'<Flow xmlns="{SF_NS}"><metadata><apiVersion>60.0</apiVersion></metadata><label>F</label></Flow>'
        ig = frozenset({"apiVersion"})
        assert normalize_xml(a, ig) == normalize_xml(b, ig)

    def test_other_differences_still_detected(self):
        a = f'<ApexClass xmlns="{SF_NS}"><apiVersion>59.0</apiVersion><status>Active</status></ApexClass>'
        b = f'<ApexClass xmlns="{SF_NS}"><apiVersion>59.0</apiVersion><status>Inactive</status></ApexClass>'
        ig = frozenset({"apiVersion"})
        assert normalize_xml(a, ig) != normalize_xml(b, ig)

    def test_compare_trees_threads_ignore_elements(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        _write(left / "classes" / "A.cls-meta.xml",
               f'<ApexClass xmlns="{SF_NS}"><apiVersion>59.0</apiVersion></ApexClass>')
        _write(right / "classes" / "A.cls-meta.xml",
               f'<ApexClass xmlns="{SF_NS}"><apiVersion>61.0</apiVersion></ApexClass>')

        plain = compare_trees(left, right)
        assert len(plain.differ_pairs) == 1

        with_rule = compare_trees(left, right, xml_ignore_elements=frozenset({"apiVersion"}))
        assert with_rule.differ_pairs == ()
        assert with_rule.identical_count == 1

    def test_xml_ignore_elements_accessor(self):
        b = default_baseline()
        b["ignore"]["xml_elements"] = ["apiVersion", "packageVersions"]
        assert xml_ignore_elements(b) == frozenset({"apiVersion", "packageVersions"})


class TestCollectReverseSyncFiles:
    """Reverse-sync pulls the org side of the drift: changed files (right copy)
    + org-only files, minus baseline-ignored/accepted."""

    def _compare(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        _write(left / "classes" / "Changed.cls", "v1")
        _write(right / "classes" / "Changed.cls", "v2")
        _write(right / "classes" / "OrgOnly.cls", "org")
        _write(left / "classes" / "RepoOnly.cls", "repo")
        _write(left / "reports" / "R.report-meta.xml", "r1")
        _write(right / "reports" / "R.report-meta.xml", "r2")
        return compare_trees(left, right), left, right

    def test_collects_differ_right_copy_and_only_right(self, tmp_path):
        from mct.delta import collect_reverse_sync_files

        result, _, right = self._compare(tmp_path)
        files = collect_reverse_sync_files(result, default_baseline())
        displays = sorted(d for _, d in files)
        assert displays == [
            "classes/Changed.cls", "classes/OrgOnly.cls", "reports/R.report-meta.xml",
        ]
        # abs paths must come from the RIGHT tree (the org side)
        assert all(str(p).startswith(str(right)) for p, _ in files)

    def test_baseline_ignored_and_accepted_excluded(self, tmp_path):
        from mct.delta import collect_reverse_sync_files

        result, left, right = self._compare(tmp_path)
        bl_file = tmp_path / "baseline.json"
        bl = default_baseline()
        bl["ignore"]["types"] = ["reports"]
        save_baseline(bl, bl_file)
        accept_diff("classes/OrgOnly.cls", None, right / "classes" / "OrgOnly.cls",
                    baseline_file=bl_file)

        files = collect_reverse_sync_files(result, load_baseline(bl_file))
        assert [d for _, d in files] == ["classes/Changed.cls"]


class TestScopedXmlElementIgnores:
    """ignore.xml_elements entries of the form "type:element" apply only to
    files whose first path segment matches the type (case-insensitive)."""

    def _baseline(self):
        data = default_baseline()
        data["ignore"]["xml_elements"] = ["apiVersion", "omniScripts:isActive"]
        return data

    def test_global_accessor_excludes_scoped_entries(self):
        assert xml_ignore_elements(self._baseline()) == frozenset({"apiVersion"})

    def test_by_type_accessor(self):
        from mct.baseline import xml_ignore_by_type
        assert xml_ignore_by_type(self._baseline()) == {"omniscripts": frozenset({"isActive"})}

    def test_effective_ignore_combines_global_and_scoped(self):
        from mct.baseline import effective_xml_ignore
        data = self._baseline()
        assert effective_xml_ignore("omniScripts/Foo_1.os-meta.xml", data) == frozenset(
            {"apiVersion", "isActive"}
        )
        assert effective_xml_ignore("classes/Bar.cls-meta.xml", data) == frozenset({"apiVersion"})

    def test_compare_trees_applies_scoped_rule_only_to_matching_type(self, tmp_path):
        left, right = tmp_path / "l", tmp_path / "r"
        os_xml = '<OmniScript xmlns="%s"><name>F</name><isActive>{}</isActive></OmniScript>' % SF_NS
        cls_xml = '<ApexClass xmlns="%s"><isActive>{}</isActive></ApexClass>' % SF_NS
        for base, val in ((left, "true"), (right, "false")):
            _write(base / "omniScripts" / "F_1.os-meta.xml", os_xml.format(val))
            _write(base / "classes" / "B.cls-meta.xml", cls_xml.format(val))
        by_type = {"omniscripts": frozenset({"isActive"})}
        result = compare_trees(left, right, None, False, xml_ignore_by_type=by_type)
        differing = {d for _, _, d, _ in result.differ_pairs}
        assert differing == {"classes/B.cls-meta.xml"}

    def test_classify_entry_uses_scoped_ignore_for_fingerprint(self, tmp_path, monkeypatch):
        import mct.config as _cfg
        monkeypatch.setattr(_cfg, "STORAGE_ROOT", tmp_path)
        bl_file = tmp_path / "baseline.json"
        data = self._baseline()
        save_baseline(data, bl_file)

        os_xml = ('<OmniScript xmlns="%s"><name>F</name><description>{}</description>'
                  '<isActive>{}</isActive></OmniScript>' % SF_NS)
        lp = _write(tmp_path / "l" / "omniScripts" / "F_1.os-meta.xml", os_xml.format("left", "true"))
        rp = _write(tmp_path / "r" / "omniScripts" / "F_1.os-meta.xml", os_xml.format("right", "false"))
        accept_diff("omniScripts/F_1.os-meta.xml", lp, rp, baseline_file=bl_file)

        # isActive flips again on the left: scoped rule says that's noise, so
        # the acceptance must NOT go stale.
        lp.write_text(os_xml.format("left", "false"), encoding="utf-8")
        status, _ = classify_entry(
            "omniScripts/F_1.os-meta.xml", lp, rp, load_baseline(bl_file)
        )
        assert status == "accepted"


class TestJsonFingerprint:
    def test_stable_across_cosmetic_json_churn(self, tmp_path):
        """The comparison treats cosmetically-reordered JSON as equal
        (json_semantically_equal), so the acceptance fingerprint must too —
        otherwise an accepted JSON diff goes accepted_stale on noise."""
        r = _write(tmp_path / "r" / "x.json", '{"a": 1, "b": 2}\n')
        l1 = _write(tmp_path / "l1" / "x.json", '{"b": 3, "a": 1}\n')
        fp1 = compute_fingerprint(l1, r)
        l2 = _write(tmp_path / "l2" / "x.json", '{\n  "a": 1,\n  "b": 3\n}\n')
        fp2 = compute_fingerprint(l2, r)
        assert fp1 == fp2

    def test_real_json_change_still_flips_fingerprint(self, tmp_path):
        r = _write(tmp_path / "r" / "x.json", '{"a": 1}\n')
        l1 = _write(tmp_path / "l1" / "x.json", '{"a": 2}\n')
        l2 = _write(tmp_path / "l2" / "x.json", '{"a": 3}\n')
        assert compute_fingerprint(l1, r) != compute_fingerprint(l2, r)


class TestTextFingerprintWhitespace:
    """Fingerprints for plain-text files must use the same equivalence as the
    comparison (text_equal_ignoring_line_endings): CRLF/LF and trailing
    whitespace churn must not invalidate an acceptance."""

    def test_trailing_newline_churn_keeps_fingerprint_stable(self, tmp_path):
        l = _write(tmp_path / "classes" / "A.cls", "public class A { Integer x; }")
        r1 = _write(tmp_path / "r1" / "A.cls", "public class A { Integer y; }")
        r2 = _write(tmp_path / "r2" / "A.cls", "public class A { Integer y; }\n")
        assert compute_fingerprint(l, r1) == compute_fingerprint(l, r2)

    def test_crlf_churn_keeps_fingerprint_stable(self, tmp_path):
        l = _write(tmp_path / "classes" / "B.cls", "public class B {\n}")
        r1 = _write(tmp_path / "r1" / "B.cls", "public class B2 {\n}")
        r2 = _write(tmp_path / "r2" / "B.cls", "public class B2 {\r\n}")
        assert compute_fingerprint(l, r1) == compute_fingerprint(l, r2)

    def test_real_text_change_still_changes_fingerprint(self, tmp_path):
        l = _write(tmp_path / "classes" / "C.cls", "public class C {}")
        r1 = _write(tmp_path / "r1" / "C.cls", "public class C1 {}")
        r2 = _write(tmp_path / "r2" / "C.cls", "public class C2 {}")
        assert compute_fingerprint(l, r1) != compute_fingerprint(l, r2)


class TestPairScopedAcceptance:
    def _seed_pair(self, tmp_path):
        lp = _write(tmp_path / "classes" / "A.cls", "public class A { Integer x; }")
        rp = _write(tmp_path / "r" / "A.cls", "public class A { Integer y; }")
        return lp, rp

    def test_pair_key_for(self):
        from mct.baseline import pair_key_for
        b = {"type": "branch", "branch": "main", "org_alias": None}
        o1 = {"type": "org_retrieve", "branch": None, "org_alias": "uat"}
        o2 = {"type": "org_retrieve", "branch": None, "org_alias": "prod"}
        assert pair_key_for(b, o2) == "branch:main↔org:prod"
        assert pair_key_for(o1, o2) == "org:uat↔org:prod"
        assert pair_key_for(None, o2) is None

    def test_acceptance_scoped_to_pair(self, tmp_path):
        lp, rp = self._seed_pair(tmp_path)
        bl_file = tmp_path / "baseline.json"
        accept_diff("classes/A.cls", lp, rp, baseline_file=bl_file,
                    pair_key="org:uat↔org:prod")
        data = load_baseline(bl_file)
        s1, _ = classify_entry("classes/A.cls", lp, rp, data,
                               pair_key="org:uat↔org:prod")
        s2, _ = classify_entry("classes/A.cls", lp, rp, data,
                               pair_key="branch:main↔org:prod")
        s3, _ = classify_entry("classes/A.cls", lp, rp, data, pair_key=None)
        assert s1 == "accepted"
        assert s2 == "active"
        assert s3 == "active"

    def test_legacy_pairless_acceptance_applies_everywhere(self, tmp_path):
        lp, rp = self._seed_pair(tmp_path)
        bl_file = tmp_path / "baseline.json"
        accept_diff("classes/A.cls", lp, rp, baseline_file=bl_file)
        data = load_baseline(bl_file)
        status, _ = classify_entry("classes/A.cls", lp, rp, data,
                                   pair_key="org:uat↔org:prod")
        assert status == "accepted"
