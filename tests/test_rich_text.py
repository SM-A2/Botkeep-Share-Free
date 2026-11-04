"""Admin-edited texts: formatting must survive (no database / Telegram needed).
Run:  python tests/test_rich_text.py"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
for name in ("config", "database", "logging_setup"):       # settings_service only needs the names
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = types.ModuleType(name)
        sys.modules[name].Config = sys.modules[name].Database = object
        sys.modules[name].log_event = lambda *a, **k: None

from services.settings_service import ValidationError, sanitize_html  # noqa: E402


def ok(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        sys.exit(1)


def bad(text, **kw):
    try:
        sanitize_html(text, **kw)
    except ValidationError:
        return True
    return False


ok("basic tags kept", sanitize_html("<b>Hi</b> <i>x</i>") == "<b>Hi</b> <i>x</i>")
ok("blockquote kept", sanitize_html("<blockquote>q</blockquote>") == "<blockquote>q</blockquote>")
ok("expandable quote kept", "<blockquote expandable>" in sanitize_html("<blockquote expandable>q</blockquote>"))
ok("https link kept", sanitize_html('<a href="https://x.com/?a=1&b=2">go</a>') ==
   '<a href="https://x.com/?a=1&amp;b=2">go</a>')
ok("javascript link refused", bad('<a href="javascript:alert(1)">go</a>'))
ok("unknown tag shown as text", sanitize_html("<script>") == "&lt;script&gt;")
ok("unbalanced refused", bad("<b>oops"))
ok("quote can be stripped (bio)", sanitize_html("<blockquote>q</blockquote>", allow_quote=False) == "q")
ok("markdown still works", sanitize_html("**b** `c`") == "<b>b</b> <code>c</code>")
print("ALL RICH TEXT TESTS PASSED")
