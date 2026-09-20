import sys

# Status output uses glyphs (─, ▶, ✓, →, ⚠) that crash under legacy console
# encodings (Windows cp1252). Degrade to escapes instead of UnicodeEncodeError;
# UTF-8 streams and non-TextIOWrapper streams (pytest capture) are untouched.
for _stream in (sys.stdout, sys.stderr):
    _encoding = getattr(_stream, "encoding", None) or ""
    if _encoding.lower() not in ("utf-8", "utf8") and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")
