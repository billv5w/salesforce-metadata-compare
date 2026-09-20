"""
Unit tests for scripts/retrieved_folder_compare.py.

Covers: norm_key, index_tree, compare_trees, TreeCompareResult.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from retrieved_folder_compare import (
    norm_key,
    index_tree,
    compare_trees,
    TreeCompareResult,
)
from pathlib import Path


# ---------------------------------------------------------------------------
# norm_key
# ---------------------------------------------------------------------------

class TestNormKey:
    def test_case_folded_on_all_platforms(self):
        """Windows and default macOS filesystems are case-insensitive; two SFDX
        files differing only by case are the same logical component."""
        assert norm_key(Path("classes/MyClass.cls")) == "classes/myclass.cls"

    def test_returns_string(self):
        assert isinstance(norm_key(Path("a/b.xml")), str)

    def test_nested_path(self):
        p = Path("a/b/c.xml")
        assert norm_key(p) == "a/b/c.xml"

    def test_single_file(self):
        assert norm_key(Path("file.xml")) == "file.xml"


# ---------------------------------------------------------------------------
# index_tree  (uses a real temp directory)
# ---------------------------------------------------------------------------

class TestIndexTree:
    def test_empty_directory(self, tmp_path):
        result = index_tree(tmp_path)
        assert result == {}

    def test_single_file(self, tmp_path):
        f = tmp_path / "MyClass.cls"
        f.write_text("public class MyClass {}")
        result = index_tree(tmp_path)
        assert len(result) == 1
        key = list(result.keys())[0]
        assert key == "myclass.cls"  # keys case-folded; display keeps casing
        abs_path, disp = result[key]
        assert abs_path == f.resolve()
        assert disp == "MyClass.cls"

    def test_nested_files(self, tmp_path):
        (tmp_path / "classes").mkdir()
        f = tmp_path / "classes" / "MyClass.cls"
        f.write_text("body")
        result = index_tree(tmp_path)
        assert "classes/myclass.cls" in result

    def test_multiple_files(self, tmp_path):
        (tmp_path / "a.xml").write_text("a")
        (tmp_path / "b.xml").write_text("b")
        result = index_tree(tmp_path)
        assert len(result) == 2
        assert "a.xml" in result
        assert "b.xml" in result

    def test_directories_not_included(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        result = index_tree(tmp_path)
        assert result == {}

    def test_display_path_is_posix(self, tmp_path):
        (tmp_path / "classes").mkdir()
        (tmp_path / "classes" / "Foo.cls").write_text("x")
        result = index_tree(tmp_path)
        _, disp = result["classes/foo.cls"]
        assert "/" in disp
        assert "\\" not in disp

    def test_abs_path_is_absolute(self, tmp_path):
        (tmp_path / "file.xml").write_text("data")
        result = index_tree(tmp_path)
        abs_path, _ = result["file.xml"]
        assert abs_path.is_absolute()


# ---------------------------------------------------------------------------
# compare_trees
# ---------------------------------------------------------------------------

class TestCompareTrees:
    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Binary write: text mode would translate \n -> \r\n on Windows, so
        # fixtures must go to disk byte-exact for line-ending assertions.
        path.write_bytes(content.encode("utf-8"))

    def test_identical_trees(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "a.xml", "<root/>")
        self._write(right / "a.xml", "<root/>")

        result = compare_trees(left, right)

        assert isinstance(result, TreeCompareResult)
        assert result.identical_count == 1
        assert result.only_left_keys == ()
        assert result.only_right_keys == ()
        assert result.differ_pairs == ()

    def test_file_only_in_left(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "a.xml", "<root/>")
        self._write(left / "b.xml", "<extra/>")
        self._write(right / "a.xml", "<root/>")
        right.mkdir(exist_ok=True)

        result = compare_trees(left, right)

        assert "b.xml" in result.only_left_keys
        assert result.only_right_keys == ()

    def test_file_only_in_right(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        left.mkdir()
        self._write(right / "new.xml", "<new/>")

        result = compare_trees(left, right)

        assert "new.xml" in result.only_right_keys
        assert result.only_left_keys == ()

    def test_same_name_different_content(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "file.xml", "<old/>")
        self._write(right / "file.xml", "<new/>")

        result = compare_trees(left, right)

        assert result.identical_count == 0
        assert len(result.differ_pairs) == 1
        lp, rp, ld, rd = result.differ_pairs[0]
        assert ld == "file.xml"
        assert rd == "file.xml"

    def test_empty_both_trees(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        left.mkdir()
        right.mkdir()

        result = compare_trees(left, right)

        assert result.identical_count == 0
        assert result.only_left_keys == ()
        assert result.only_right_keys == ()
        assert result.differ_pairs == ()

    def test_nested_file_comparison(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "classes" / "Foo.cls", "class Foo {}")
        self._write(right / "classes" / "Foo.cls", "class Foo {}")

        result = compare_trees(left, right)

        assert result.identical_count == 1
        assert result.differ_pairs == ()

    def test_nested_file_differs(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "classes" / "Foo.cls", "v1")
        self._write(right / "classes" / "Foo.cls", "v2")

        result = compare_trees(left, right)

        assert result.identical_count == 0
        assert len(result.differ_pairs) == 1

    def test_only_left_keys_sorted(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        right.mkdir()
        for name in ["z.xml", "a.xml", "m.xml"]:
            self._write(left / name, "data")

        result = compare_trees(left, right)

        keys = list(result.only_left_keys)
        assert keys == sorted(keys, key=str.lower)

    def test_result_is_frozen_dataclass(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        left.mkdir()
        right.mkdir()

        result = compare_trees(left, right)

        with pytest.raises((AttributeError, TypeError)):
            result.identical_count = 99  # type: ignore[misc]

    def test_left_ix_and_right_ix_populated(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "a.xml", "a")
        self._write(right / "b.xml", "b")

        result = compare_trees(left, right)

        assert "a.xml" in result.left_ix
        assert "b.xml" in result.right_ix

    def test_casing_only_difference_is_same_file(self, tmp_path):
        """Foo.cls vs foo.cls is the same component, not an add+delete pair."""
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "classes" / "Foo.cls", "public class Foo {}")
        self._write(right / "classes" / "foo.cls", "public class Foo {}")

        result = compare_trees(left, right)

        assert result.only_left_keys == ()
        assert result.only_right_keys == ()
        assert result.identical_count == 1


# ---------------------------------------------------------------------------
# compare_trees — XML semantic equality (false-positive reduction)
# ---------------------------------------------------------------------------

SF_NS = "http://soap.sforce.com/2006/04/metadata"


class TestCompareTreesXmlSemanticEquality:
    """XML files that differ only in element ordering or formatting must be
    classified as identical, not placed in differ_pairs."""

    @staticmethod
    def _write(path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))

    def test_reordered_xml_elements_classified_as_identical(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        # Same permissions, different order
        self._write(
            left / "permissionsets" / "MyPS.permissionset-meta.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<PermissionSet xmlns="{SF_NS}">\n'
            f'    <fieldPermissions><field>Account.Name</field><editable>true</editable></fieldPermissions>\n'
            f'    <fieldPermissions><field>Account.BillingCity</field><editable>false</editable></fieldPermissions>\n'
            f'</PermissionSet>\n',
        )
        self._write(
            right / "permissionsets" / "MyPS.permissionset-meta.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<PermissionSet xmlns="{SF_NS}">\n'
            f'    <fieldPermissions><field>Account.BillingCity</field><editable>false</editable></fieldPermissions>\n'
            f'    <fieldPermissions><field>Account.Name</field><editable>true</editable></fieldPermissions>\n'
            f'</PermissionSet>\n',
        )

        result = compare_trees(left, right)

        assert result.identical_count == 1
        assert result.differ_pairs == ()

    def test_different_indentation_classified_as_identical(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(
            left / "objects" / "Account.object-meta.xml",
            f'<CustomObject xmlns="{SF_NS}"><label>Account</label></CustomObject>',
        )
        self._write(
            right / "objects" / "Account.object-meta.xml",
            f'<CustomObject xmlns="{SF_NS}">\n    <label>Account</label>\n</CustomObject>',
        )

        result = compare_trees(left, right)

        assert result.identical_count == 1
        assert result.differ_pairs == ()

    def test_genuine_xml_difference_classified_as_different(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(
            left / "permissionsets" / "MyPS.permissionset-meta.xml",
            f'<PermissionSet xmlns="{SF_NS}"><fieldPermissions><field>X</field><editable>true</editable></fieldPermissions></PermissionSet>',
        )
        self._write(
            right / "permissionsets" / "MyPS.permissionset-meta.xml",
            f'<PermissionSet xmlns="{SF_NS}"><fieldPermissions><field>X</field><editable>false</editable></fieldPermissions></PermissionSet>',
        )

        result = compare_trees(left, right)

        assert result.identical_count == 0
        assert len(result.differ_pairs) == 1

    def test_known_text_suffix_crlf_is_formatting_only(self, tmp_path):
        """Text metadata (e.g. .cls) differing only by line endings is normalised:
        the Metadata API strips trailing newlines / changes endings on retrieve,
        so this is noise, not a real diff (see TestTextLineEndingNormalization)."""
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "classes" / "Foo.cls", "public class Foo {}\n")
        (right / "classes").mkdir(parents=True, exist_ok=True)
        (right / "classes" / "Foo.cls").write_bytes(b"public class Foo {}\r\n")

        result = compare_trees(left, right)

        assert result.identical_count == 1
        assert result.differ_pairs == ()
        assert len(result.identical_normalized_pairs) == 1

    def test_crlf_xml_classified_as_identical_to_lf_xml(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        base_xml = f'<PermissionSet xmlns="{SF_NS}"><objectPermissions><object>Account</object></objectPermissions></PermissionSet>'
        self._write(left / "ps.xml", base_xml.replace("\n", "\r\n"))
        (right / "ps.xml").parent.mkdir(parents=True, exist_ok=True)
        (right / "ps.xml").write_bytes(base_xml.encode())

        result = compare_trees(left, right)

        assert result.identical_count == 1
        assert result.differ_pairs == ()


class TestCompareTreesJsonSemanticEquality:
    """JSON files that differ only in formatting or key ordering must be
    classified as identical, not placed in differ_pairs."""

    @staticmethod
    def _write(path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))

    def test_reordered_json_keys_classified_as_identical(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "data.json", '{"b": 2, "a": 1}')
        self._write(right / "data.json", '{\n  "a": 1,\n  "b": 2\n}')

        result = compare_trees(left, right)

        assert result.identical_count == 1
        assert result.differ_pairs == ()

    def test_reordered_dict_arrays_classified_as_different(self, tmp_path):
        """Array order is semantic — a reordered JSON array is real drift."""
        left = tmp_path / "left"
        right = tmp_path / "right"
        self._write(left / "data.json", '{"items": [{"name": "z"}, {"name": "a"}]}')
        self._write(right / "data.json", '{"items": [{"name": "a"}, {"name": "z"}]}')

        result = compare_trees(left, right)

        assert result.identical_count == 0
        assert len(result.differ_pairs) == 1



# ---------------------------------------------------------------------------
# text line-ending normalization (Metadata API strips trailing newlines)
# ---------------------------------------------------------------------------

class TestTextLineEndingNormalization:
    def _trees(self, tmp_path, left_bytes, right_bytes, name="classes/MyClass.cls"):
        left = tmp_path / "left"
        right = tmp_path / "right"
        for root, content in ((left, left_bytes), (right, right_bytes)):
            f = root / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(content)
        return left, right

    def test_trailing_newline_only_is_identical(self, tmp_path):
        left, right = self._trees(tmp_path, b"public class A {\n}\n", b"public class A {\n}")
        res = compare_trees(left, right)
        assert res.identical_count == 1
        assert res.differ_pairs == ()
        assert len(res.identical_normalized_pairs) == 1

    def test_crlf_vs_lf_is_identical(self, tmp_path):
        left, right = self._trees(tmp_path, b"a {\r\n}\r\n", b"a {\n}\n", name="x.css")
        res = compare_trees(left, right)
        assert res.identical_count == 1
        assert res.differ_pairs == ()

    def test_real_content_change_still_differs(self, tmp_path):
        left, right = self._trees(tmp_path, b"public class A {\n}\n", b"public class B {\n}\n")
        res = compare_trees(left, right)
        assert res.identical_count == 0
        assert len(res.differ_pairs) == 1

    def test_unknown_suffix_not_normalized(self, tmp_path):
        left, right = self._trees(tmp_path, b"BIN\n", b"BIN", name="thing.resource")
        res = compare_trees(left, right)
        assert res.identical_count == 0
        assert len(res.differ_pairs) == 1


# ---------------------------------------------------------------------------
# dotfile exclusion (.gitkeep etc. are git placeholders, never org content)
# ---------------------------------------------------------------------------

class TestDotfileExclusion:
    def test_gitkeep_not_indexed_or_reported(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        (left / "classes").mkdir(parents=True)
        (right / "classes").mkdir(parents=True)
        (left / "classes" / "A.cls").write_text("public class A {}\n")
        (right / "classes" / "A.cls").write_text("public class A {}\n")
        (left / ".gitkeep").write_text("")
        (left / "classes" / ".forceignore").write_text("x")

        res = compare_trees(left, right)

        assert res.only_left_keys == ()
        assert res.only_right_keys == ()
        assert res.identical_count == 1
        assert res.differ_pairs == ()
