# SPDX-License-Identifier: MIT
"""Print a value from config.toml.

    config.py KEY            a dotted key, e.g. "kernel.version"
    config.py --keys TABLE   the keys of a table, one per line

Lists print one item per line. Exits 1 if the key is missing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lapin import load_config  # noqa: E402


def lookup(config, dotted):
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def main(argv):
    config = load_config()
    try:
        if argv[:1] == ["--keys"]:
            for key in lookup(config, argv[1]):
                print(key)
            return 0
        value = lookup(config, argv[0])
    except (KeyError, IndexError):
        print(f"config.toml: no {' '.join(argv)}", file=sys.stderr)
        return 1
    if isinstance(value, list):
        for item in value:
            print(item)
    elif isinstance(value, bool):
        print("true" if value else "false")
    else:
        print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
