# SPDX-License-Identifier: MIT
"""controller: the language of speech and speech recognition that name none.

The install writes the rabbit's language (LANGUAGE= in lapinlibre.txt) to
karotz.conf, as does the platform's configuration (here fr-FR); the platform
account (account.conf) has only an id and a password from the install, and
here says it-IT. In interactive mode, a tts request and an asr request
without a language must use karotz.conf's, fr-FR, not the account's; a
request that names one keeps it. The controller keeps running.
"""

import re
from pathlib import Path

from scenarios import controller_platform as base

DAEMON = "controller"
DEVICES = []
normalise_record = base.normalise_record


def sandbox_setup(workdir, sysroot):
    setup = base.sandbox_setup(workdir, sysroot)
    for account in (Path(workdir) / "conf-usr" / "account.conf", Path(workdir) / "var" / "tmp" / "account.conf"):
        text, n = re.subn(r"^lang=.*$", "lang=it-IT", account.read_text(), flags=re.M)
        assert n == 1
        account.write_text(text)
    return setup


def trigger(platform, p, _fake, step):
    platform.send(p, 'id: "p-say" interactiveId: "ia-1" tts { action: START text: "hallo" }')
    step("tts without a language", 'correlationId: "p-say"', "tts_speak", settle=1.0, timeout=30)
    platform.send(p, 'id: "p-say-en" interactiveId: "ia-1" tts { action: START text: "hello" lang: "en-US" }')
    step("tts with a language", 'correlationId: "p-say-en"', "tts_speak", settle=1.0, timeout=30)
    platform.send(p, 'id: "p-asr" interactiveId: "ia-1" asr { dictionary: "ja" dictionary: "nein" }')
    step("asr without a language", 'correlationId: "p-asr"', "asr_start_string", settle=1.0, timeout=30)


def drive(d, bus):
    return base.drive(d, bus, trigger=trigger, when="interactive")


def check(record):
    text = "\n".join(record)
    problems = []
    if "== daemon still running: True" not in record:
        problems.append("the controller didn't survive")
    for call in ('tts_speak("p-say", "hallo", "fr-FR", 1)', 'tts_speak("p-say-en", "hello", "en-US", 1)',
                 'asr_start_string("p-asr", "ja | nein", "fr-FR", 1)'):
        if call not in text:
            found = [l for l in record if "tts_speak" in l or "asr_start_string" in l]
            problems.append(f"no {call} (the calls: {found})")
    return problems
