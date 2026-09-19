import tempfile
from pathlib import Path
from scripts.json_normalizer import normalize_json, json_semantically_equal


def test_normalize_json_basic():
    # Simple object key sorting
    raw = '{"b": 2, "a": 1}'
    expected = '{\n    "a": 1,\n    "b": 2\n}\n'
    assert normalize_json(raw) == expected


def test_normalize_json_nested():
    # Key sorting recursively in nested objects
    raw = '{"z": 10, "nested": {"y": 2, "x": 1}}'
    expected = (
        '{\n'
        '    "nested": {\n'
        '        "x": 1,\n'
        '        "y": 2\n'
        '    },\n'
        '    "z": 10\n'
        '}\n'
    )
    assert normalize_json(raw) == expected


def test_normalize_json_list_order_preserved():
    # Array order is semantic (same stance as embedded-JSON in XML):
    # dict arrays are NOT reordered, even when an identity key exists.
    raw = '{"items": [{"fullName": "beta", "val": 2}, {"fullName": "alpha", "val": 1}]}'
    res = normalize_json(raw)
    assert res.index("beta") < res.index("alpha")


def test_normalize_json_array_order_is_content():
    a = '{"items": [{"name": "b"}, {"name": "a"}]}'
    b = '{"items": [{"name": "a"}, {"name": "b"}]}'
    assert normalize_json(a) != normalize_json(b)


def test_normalize_json_keys_still_sorted():
    a = '{"z": 1, "a": {"y": 2, "b": 3}}'
    out = normalize_json(a)
    assert out.index('"a"') < out.index('"z"')
    assert out.index('"b"') < out.index('"y"')


def test_normalize_json_malformed():
    # Fallback to input string on invalid JSON
    raw = "not a valid json {{"
    assert normalize_json(raw) == raw


def test_json_semantically_equal():
    with tempfile.TemporaryDirectory() as tmpdir:
        p1 = Path(tmpdir) / "f1.json"
        p2 = Path(tmpdir) / "f2.json"

        # Same content, different key order / formatting: equal.
        p1.write_text('{"b": 2, "items": [{"fullName": "a"}, {"fullName": "b"}], "a": 1}')
        p2.write_text('{\n  "a": 1,\n  "b": 2,\n  "items": [\n    {"fullName": "a"},\n    {"fullName": "b"}\n  ]\n}')
        assert json_semantically_equal(p1, p2) is True

        # Different ARRAY order: not equal (order is content).
        p2.write_text('{"a": 1, "b": 2, "items": [{"fullName": "b"}, {"fullName": "a"}]}')
        assert json_semantically_equal(p1, p2) is False
