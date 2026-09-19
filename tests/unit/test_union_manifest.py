"""Unit tests for the bidirectional union manifest (mct/snapshot.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mct.snapshot import build_union_manifest, parse_manifest_types, write_manifest

SF_NS = "http://soap.sforce.com/2006/04/metadata"


def _manifest(path: Path, members_by_type: dict[str, list[str]], version: str = "66.0") -> Path:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', f'<Package xmlns="{SF_NS}">']
    for t, members in members_by_type.items():
        parts.append("    <types>")
        for m in members:
            parts.append(f"        <members>{m}</members>")
        parts.append(f"        <name>{t}</name>")
        parts.append("    </types>")
    parts.append(f"    <version>{version}</version>")
    parts.append("</Package>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return path


class TestParseManifestTypes:
    def test_parses_types_and_members(self, tmp_path):
        p = _manifest(tmp_path / "p.xml", {"ApexClass": ["Foo", "Bar"], "Flow": ["MyFlow"]})
        out = parse_manifest_types(p)
        assert out == {"ApexClass": {"Foo", "Bar"}, "Flow": {"MyFlow"}}

    def test_wildcard_member_preserved(self, tmp_path):
        p = _manifest(tmp_path / "p.xml", {"ApexClass": ["*"]})
        assert parse_manifest_types(p) == {"ApexClass": {"*"}}

    def test_write_parse_roundtrip(self, tmp_path):
        original = {"ApexClass": {"Foo"}, "CustomObject": {"Account", "Case"}}
        out = tmp_path / "out.xml"
        write_manifest(out, original, "66.0")
        assert parse_manifest_types(out) == original
        assert "<version>66.0</version>" in out.read_text(encoding="utf-8")

    def test_write_escapes_xml_special_chars(self, tmp_path):
        out = tmp_path / "out.xml"
        write_manifest(out, {"Layout": {"Account-Sales & Marketing"}}, "66.0")
        text = out.read_text(encoding="utf-8")
        assert "&amp;" in text
        assert parse_manifest_types(out) == {"Layout": {"Account-Sales & Marketing"}}


class TestBuildUnionManifest:
    def test_org_members_of_source_types_are_added(self, tmp_path):
        src = _manifest(tmp_path / "src.xml", {"ApexClass": ["Foo"]})
        org = _manifest(tmp_path / "org.xml", {"ApexClass": ["Foo", "OrgOnly"]})
        out = tmp_path / "union.xml"

        stats = build_union_manifest(src, org, out, "66.0")

        assert parse_manifest_types(out) == {"ApexClass": {"Foo", "OrgOnly"}}
        assert stats["union_types"] == 1
        assert stats["union_members"] == 2

    def test_org_only_types_skipped_by_default(self, tmp_path):
        """Untracked org types (Report, Layout...) must not flood the retrieve."""
        src = _manifest(tmp_path / "src.xml", {"ApexClass": ["Foo"]})
        org = _manifest(
            tmp_path / "org.xml",
            {"ApexClass": ["Foo"], "Report": ["R1", "R2"], "Layout": ["Account-L"]},
        )
        out = tmp_path / "union.xml"

        stats = build_union_manifest(src, org, out, "66.0")

        assert parse_manifest_types(out) == {"ApexClass": {"Foo"}}
        assert stats["skipped_org_types"] == ["Layout", "Report"]

    def test_extra_org_types_opt_in(self, tmp_path):
        src = _manifest(tmp_path / "src.xml", {"ApexClass": ["Foo"]})
        org = _manifest(tmp_path / "org.xml", {"ApexClass": ["Foo"], "Report": ["R1"]})
        out = tmp_path / "union.xml"

        stats = build_union_manifest(src, org, out, "66.0", extra_org_types=["Report"])

        assert parse_manifest_types(out) == {"ApexClass": {"Foo"}, "Report": {"R1"}}
        assert stats["skipped_org_types"] == []

    def test_source_only_types_always_kept(self, tmp_path):
        """Components deleted in the org must still be retrieved-for (only_left)."""
        src = _manifest(tmp_path / "src.xml", {"ApexClass": ["Foo"], "Flow": ["Gone"]})
        org = _manifest(tmp_path / "org.xml", {"ApexClass": ["Foo"]})
        out = tmp_path / "union.xml"

        build_union_manifest(src, org, out, "66.0")

        assert parse_manifest_types(out) == {"ApexClass": {"Foo"}, "Flow": {"Gone"}}

    def test_version_stamped_from_api_version(self, tmp_path):
        src = _manifest(tmp_path / "src.xml", {"ApexClass": ["Foo"]}, version="58.0")
        org = _manifest(tmp_path / "org.xml", {"ApexClass": ["Foo"]}, version="59.0")
        out = tmp_path / "union.xml"

        build_union_manifest(src, org, out, "66.0")

        assert "<version>66.0</version>" in out.read_text(encoding="utf-8")


class TestParseManifestRobustness:
    def test_malformed_xml_raises_clear_error(self, tmp_path):
        p = tmp_path / "package.xml"
        p.write_text("<Package><types><name>ApexClass", encoding="utf-8")
        with pytest.raises(RuntimeError, match="package.xml"):
            parse_manifest_types(p)

    def test_missing_file_raises_clear_error(self, tmp_path):
        with pytest.raises(RuntimeError, match="nope.xml"):
            parse_manifest_types(tmp_path / "nope.xml")
