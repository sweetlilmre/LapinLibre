# SPDX-License-Identifier: MIT
"""karotzVM's JavaScript API: every function it gives applications is called by a test application.

The functions are read from karotzVM's source (every js_set_function and every social function), and
each one's full name (for example karotz.led.light) is looked for, followed by "(", in the scenarios'
and aggressive tests' applications and in the example applications. A function no test calls fails
the test unless it is in EXCEPTIONS, with the reason. Run directly, it prints every function and
whether a test calls it:

    python3 -I -B tests/image/test_karotzvm_api.py --list
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KVM = ROOT / "userland" / "src" / "usr" / "karotzVM"
SOURCES = [ROOT / "tests" / "scenarios", ROOT / "tests" / "aggressive", ROOT / "tools" / "app-server" / "example-apps"]

# Functions no test calls, each with the reason.
EXCEPTIONS = {
}

SET_FUNCTION = re.compile(r'js_set_function\((\w+),\s*"(\w+)"')
TEMPLATE = re.compile(r"v8::Handle<v8::ObjectTemplate>\s+(\w+)_template\(\)")
ACCESS = re.compile(r'\{"(\w+)",\s*(\w+)_template\}')
SOCIAL = re.compile(r'(\w+)->Set\(v8::String::New\("(\w+)"\),\s*new_function\(')


def api_functions():
    """Every function karotzVM gives applications, by its full name."""
    names = set()
    for path in sorted(KVM.glob("*.cc")):
        text = path.read_text()
        owners = {}
        for m in TEMPLATE.finditer(text):
            owners[m.start()] = m.group(1)
        for m in SET_FUNCTION.finditer(text):
            obj, fn = m.groups()
            if obj == "global":
                names.add(fn)
            elif obj == "karotz_object":
                names.add(f"karotz.{fn}")
            elif obj == "tmpl":
                before = [o for o in owners if o < m.start()]
                if not before:
                    continue
                names.add(f"{owners[max(before)]}.{fn}")
        for m in SOCIAL.finditer(text):
            names.add(f"social.{m.group(1)}.{m.group(2)}")
    app = (KVM / "app.cc").read_text()
    on_karotz = {template: access for access, template in ACCESS.findall(app)}
    full = set()
    for name in names:
        head, _, rest = name.partition(".")
        if rest and head in on_karotz:
            full.add(f"karotz.{on_karotz[head]}.{rest}")
        else:
            full.add(name)
    return full


def test_text():
    parts = []
    for folder in SOURCES:
        for path in sorted(folder.rglob("*")):
            if path.suffix in (".py", ".js") and "__pycache__" not in path.parts:
                parts.append(path.read_text(errors="replace"))
    return "\n".join(parts)


def called(name, text):
    """Whether the text calls the function, as a.b.c( or a.b["c"]( (for names like delete and new)."""
    head, _, last = name.rpartition(".")
    forms = [re.escape(name)]
    if head:
        forms.append(re.escape(head) + r'\[\s*["\']' + re.escape(last) + r'["\']\s*\]')
    return re.search(r"(?<![\w.])(?:" + "|".join(forms) + r")\s*\(", text) is not None


class KarotzVMApiTest(unittest.TestCase):
    def test_source_is_read(self):
        names = api_functions()
        for name in ("log", "karotz.connect", "karotz.led.light", "karotz.serial.open__", "http.get",
                     "file.read", "social.init", "social.facebook.post"):
            self.assertIn(name, names)

    def test_every_function_is_called(self):
        text = test_text()
        names = api_functions()
        missing = sorted(n for n in names if n not in EXCEPTIONS and not called(n, text))
        self.assertEqual(missing, [], "karotzVM functions no test application calls (add a test, or an exception with its reason)")

    def test_exceptions_are_current(self):
        text = test_text()
        names = api_functions()
        stale = sorted(n for n in EXCEPTIONS if n not in names or called(n, text))
        self.assertEqual(stale, [], "exceptions that are no longer karotzVM functions, or that a test now calls")


if __name__ == "__main__":
    if "--list" in sys.argv:
        text = test_text()
        for name in sorted(api_functions()):
            mark = "called" if called(name, text) else ("exception" if name in EXCEPTIONS else "NOT CALLED")
            print(f"{mark:10}  {name}")
        sys.exit(0)
    unittest.main()
