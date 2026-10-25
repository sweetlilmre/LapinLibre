# SPDX-License-Identifier: MIT
"""Fixed karotz.conf and voice.conf for the scenarios, so a record doesn't depend on the settings a
sysroot or an image ships (the image's defaults name no one's servers).

sandbox_setup(workdir, sysroot, karotz=..., voice=...) copies the sysroot's /usr/etc/conf, writes
the given files over the copies and returns the shell command that mounts the copy over the
sysroot's /usr/etc/conf in the sandbox.
"""

import shutil
from pathlib import Path

# Names that never resolve (RFC 2606), so the platform and update connections fail on the name, as
# with a dead server.
KAROTZ_CONF = ("[karotz]\nip_server=platform.invalid\nport_server=9123\nurl_update=http://update.invalid/eabi\n"
               "url_app=http://apps.invalid\ndomain_app=apps.invalid\nlang=fr-FR\nasr_threshold=30\n"
               "[k2k]\nsipDomain=sip.invalid\n")
# Two speech servers on documentation addresses (RFC 5737), which tests/scenarios/voice_daemon.py
# fakes inside the daemon's network namespace.
SERVER_ADDRESSES = ["192.0.2.64", "192.0.2.65"]
VOICE_CONF = ("[tts]\nfr=http://192.0.2.65/tts/fr\nen=http://192.0.2.64/tts/en\nde=http://192.0.2.64/tts/de\n"
              "es=http://192.0.2.65/tts/es\n[asr]\nnbserver=2\nserver0=192.0.2.64\nserver1=192.0.2.65\nport=443\n")


def mount_tool():
    path = shutil.which("mount") or shutil.which("mount", path="/usr/sbin:/sbin")
    if path is None:
        raise RuntimeError("the scenarios' settings need mount")
    return path


def sandbox_setup(workdir, sysroot, karotz=None, voice=None):
    sysroot = Path(sysroot).resolve()
    conf = Path(workdir) / "conf-fixture"
    shutil.copytree(sysroot / "usr" / "etc" / "conf", conf, symlinks=True)
    for name, text in (("karotz.conf", karotz), ("voice.conf", voice)):
        if text is not None:
            (conf / name).unlink(missing_ok=True)
            (conf / name).write_text(text)
    return f"{mount_tool()} --bind {conf} {sysroot / 'usr' / 'etc' / 'conf'}; "
