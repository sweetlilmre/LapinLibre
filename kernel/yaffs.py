# SPDX-License-Identifier: GPL-2.0-only
"""Add YAFFS2 to a Linux 2.6.28 tree from an exported YAFFS2 git tree.

    yaffs.py YAFFS_TREE LINUX_TREE

Copies the Linux sources (yaffs_*.c/h and the shared headers) into
fs/yaffs2, with Makefile.kernel as its Makefile and its Kconfig, and hooks
the directory into fs/Kconfig (next to JFFS2) and fs/Makefile, as YAFFS2's
own patch-ker.sh does. Then makes the one change YAFFS2's yaffs_fs.c needs
for 2.6.28: it uses grab_cache_page_write_begin() only for kernels after
2.6.28, but 2.6.28 already replaced __grab_cache_page(), so the version
test becomes inclusive.
"""

import re
import shutil
import sys
from pathlib import Path

SHARED = ("devextras.h", "yportenv.h", "yaffsinterface.h", "moduleconfig.h", "Kconfig")
WRITE_BEGIN_TEST = re.compile(
    r"(#if\s*\(?\s*LINUX_VERSION_CODE\s*)>(\s*KERNEL_VERSION\(\s*2\s*,\s*6\s*,\s*28\s*\)\s*\)?\s*\n"
    r"\s*pg\s*=\s*grab_cache_page_write_begin\()")


def main(argv):
    yaffs, tree = Path(argv[0]), Path(argv[1])
    dest = tree / "fs" / "yaffs2"
    dest.mkdir(parents=True, exist_ok=True)
    for f in sorted(yaffs.iterdir()):
        if f.is_file() and ((f.name.startswith("yaffs") and f.suffix in (".c", ".h")) or f.name in SHARED):
            shutil.copyfile(f, dest / f.name)
    shutil.copyfile(yaffs / "Makefile.kernel", dest / "Makefile")

    kconfig = tree / "fs" / "Kconfig"
    text = kconfig.read_text()
    marker = 'source "fs/jffs2/Kconfig"'
    if "fs/yaffs2/Kconfig" not in text:
        if marker in text:
            text = text.replace(marker, marker + '\n# Patched by YAFFS\nsource "fs/yaffs2/Kconfig"\n', 1)
        else:
            text = text.replace("config JFFS2_FS", 'source "fs/yaffs2/Kconfig"\n\nconfig JFFS2_FS', 1)
        kconfig.write_text(text)
    makefile = tree / "fs" / "Makefile"
    text = makefile.read_text()
    if "yaffs2/" not in text:
        makefile.write_text(text + "\n# Patched by YAFFS\nobj-$(CONFIG_YAFFS_FS)\t\t+= yaffs2/\n")

    source = dest / "yaffs_fs.c"
    text, n = WRITE_BEGIN_TEST.subn(r"\1>=\2", source.read_text())
    if n != 1:
        raise SystemExit("yaffs_fs.c: the grab_cache_page_write_begin() version test was not found")
    source.write_text(text)
    print(f"yaffs2 -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
