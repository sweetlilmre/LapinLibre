# SPDX-License-Identifier: MIT
"""controller: installed applications whose descriptor.xml can't be used.

A descriptor that isn't valid XML, or has no <version>, makes the
controller take the application as not installed and download it again,
which replaces the descriptor. The set-up is controller_platform's, up to
the platform's connection; here the real unzip runs (controller_platform
stubs it), so the replacement can be checked: the record ends with each
descriptor's content.
"""

import zipfile
from pathlib import Path

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
normalise_record = base.normalise_record

# Truncated XML, and valid XML without <version>.
BROKEN = {"app-corrupt": '<?xml version="1.0"?>\n<descriptor><version>1</vers',
          "app-noversion": '<?xml version="1.0"?>\n<descriptor><name>x</name></descriptor>\n'}


def sandbox_setup(workdir, sysroot):
    setup = base.sandbox_setup(workdir, sysroot)
    w = Path(workdir)
    for key, text in BROKEN.items():
        (w / "apps" / key).mkdir()
        (w / "apps" / key / "descriptor.xml").write_text(text)
        archive = w / "http" / "file" / key / "1" / "get"
        archive.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("descriptor.xml", base.descriptor("1"))
            z.writestr("main.js", 'log("repaired");\n')
    # The real unzip, under qemu and natively. controller_platform
    # bind-mounts true over it (which a qemu run's system() would reach);
    # devtrace runs a command of a native run only when $KAROTZ_RUN_STUBS
    # names it and a file of that name is in the stub directory
    # (stub-bin//bin/unzip for "/bin/unzip").
    unzip = base.shutil.which("unzip")
    if unzip:
        stub = f"--bind {base.tool('true')} {Path(unzip).resolve()}; "
        assert stub in setup, "controller_platform's unzip stub changed"
        setup = setup.replace(stub, "--bind /dev/null /dev/null; ")
    marker = w / "stub-bin" / "bin" / "unzip"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("#!/bin/sh\nexit 0\n")
    marker.chmod(0o755)
    run_stubs = "export KAROTZ_RUN_STUBS=killall; "
    assert run_stubs in setup, "controller_platform's KAROTZ_RUN_STUBS changed"
    return setup.replace(run_stubs, "export KAROTZ_RUN_STUBS=killall,/bin/unzip; ")


def trigger(platform, p, fake, step):
    platform.send(p, f'id: "p-auth" authenticationToken {{ token: "{base.TOKEN}" }}')
    step("authentication", 'correlationId: "p-auth"')
    platform.send(p, 'id: "p-config" config { '
                     'configapp { apiKey: "app-corrupt" version: "1" name: "Corrupt" deployment: "hosted" } '
                     'configapp { apiKey: "app-noversion" version: "1" name: "NoVersion" deployment: "hosted" } '
                     'lang: "fr-FR" playsounds: true }')
    step("configuration with two unusable descriptors", "vm start", settle=3.0, timeout=30)


def drive(d, bus):
    record = base.drive(d, bus, trigger=trigger, when="connected")
    record.append("== descriptors afterwards")
    for key in BROKEN:
        path = Path(d.workdir) / "apps" / key / "descriptor.xml"
        text = path.read_text().strip().replace("\n", " | ") if path.exists() else "(missing)"
        record.append(f"{key}: {text}")
    return record
