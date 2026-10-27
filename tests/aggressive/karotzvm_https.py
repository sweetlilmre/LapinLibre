# SPDX-License-Identifier: MIT
"""karotzVM: HTTPS certificate checks.

The helper serves 127.0.0.1:8088's pages over HTTPS on four ports, with certificates the test makes
with the host's openssl: valid (signed by a test CA, for 127.0.0.1), self-signed, expired (signed by
the test CA, valid in 2020 only) and for another name (signed by the test CA). karotzVM finds the
test CA in $SSL_CERT_DIR. One application ("https") requests each, and plain HTTP, in three runs:

- checks: the valid certificate is taken, the other three are refused ("curl error");
- clock: karotzVM's clock starts in 1970, as the rabbit's is at boot before ntpd, and is set
  CLOCK_SET_S later: the first HTTPS request waits for it, then it is as in "checks";
- unset: the clock stays in 1970: the first HTTPS request waits for it until karotzVM gives up,
  then every certificate is refused, the valid one too (not valid yet).

ARM only, like the karotzVM scenario.
"""

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from scenarios import karotzVM as kvm

DAEMON = "karotzVM"
DEVICES = []

PORTS = {"valid": 8443, "selfsigned": 8444, "expired": 8445, "wronghost": 8446}
HELLO = '"hello from 127.0.0.1"'
REFUSED = '"curl error"'
EXPECTED = {
    "checks": {"valid": HELLO, "selfsigned": REFUSED, "expired": REFUSED, "wronghost": REFUSED, "plain": HELLO},
    "clock": {"valid": HELLO, "selfsigned": REFUSED, "expired": REFUSED, "wronghost": REFUSED, "plain": HELLO},
    "unset": {"valid": REFUSED, "selfsigned": REFUSED, "expired": REFUSED, "wronghost": REFUSED, "plain": HELLO},
}
# karotzVM's log lines for a wait for the clock, and for one that ran out; which runs have them.
WAITS = "the HTTPS request waits for it"
GAVE_UP = "certificates can't be checked"
WAITED = {"checks": (False, False), "clock": (True, False), "unset": (True, True)}
# When the clock run sets the clock: after karotzVM's start under qemu and the first request.
CLOCK_SET_S = 40
# About a day after the epoch.
BACK_TO_1970 = -(int(time.time()) - 86400)

HTTPS_JS = kvm.PRELUDE + r"""
var ports = %s;
add(function () {
    for (var name in ports)
        (function (name) {
            attempt(name, function () { return http.get("https://127.0.0.1:" + ports[name] + "/text"); });
        })(name);
    attempt("plain", function () { return http.get("http://127.0.0.1:8088/text"); });
    next();
});
add(function () { log("https done"); exit(); });
start();
""" % str(PORTS).replace("'", '"')

APPS = {"https": {"accesses": kvm.ALL_ACCESSES, "main": HTTPS_JS}}
LAUNCHES = [("https manual", 180)]

CA_CONFIG = """[ca]
default_ca = test
[test]
database = index.txt
new_certs_dir = .
serial = serial
default_md = sha256
policy = any
copy_extensions = copy
[any]
commonName = supplied
"""


def openssl(folder, *args):
    subprocess.run(["openssl", *args], cwd=folder, check=True, capture_output=True, timeout=60)


def make_certificates(folder):
    """{name: (certificate, key)} for PORTS, and the test CA's directory (named by its hash)."""
    ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    openssl(folder, "req", "-x509", *ec, "-keyout", "ca.key", "-out", "ca.pem", "-days", "3650",
            "-subj", "/CN=LapinLibre test CA", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    (folder / "ip.ext").write_text("subjectAltName=IP:127.0.0.1\n")
    (folder / "other.ext").write_text("subjectAltName=DNS:other.example\n")
    out = {}
    for name, ext in (("valid", "ip.ext"), ("wronghost", "other.ext")):
        openssl(folder, "req", "-new", *ec, "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}")
        openssl(folder, "x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                "-CAcreateserial", "-days", "3650", "-extfile", ext, "-out", f"{name}.pem")
        out[name] = (folder / f"{name}.pem", folder / f"{name}.key")
    openssl(folder, "req", "-x509", *ec, "-keyout", "selfsigned.key", "-out", "selfsigned.pem", "-days", "3650",
            "-subj", "/CN=selfsigned", "-addext", "subjectAltName=IP:127.0.0.1")
    out["selfsigned"] = (folder / "selfsigned.pem", folder / "selfsigned.key")
    (folder / "ca.cnf").write_text(CA_CONFIG)
    (folder / "index.txt").write_text("")
    (folder / "serial").write_text("1000\n")
    openssl(folder, "req", "-new", *ec, "-keyout", "expired.key", "-out", "expired.csr", "-subj", "/CN=expired",
            "-addext", "subjectAltName=IP:127.0.0.1")
    openssl(folder, "ca", "-config", "ca.cnf", "-batch", "-notext", "-cert", "ca.pem", "-keyfile", "ca.key",
            "-in", "expired.csr", "-out", "expired.pem", "-startdate", "20200101000000Z", "-enddate", "20200102000000Z")
    out["expired"] = (folder / "expired.pem", folder / "expired.key")
    ca_dir = folder / "ca"
    ca_dir.mkdir()
    h = subprocess.run(["openssl", "x509", "-noout", "-subject_hash", "-in", str(folder / "ca.pem")],
                       check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    shutil.copy(folder / "ca.pem", ca_dir / f"{h}.0")
    return out, ca_dir


def run(d, bus, certs, env, shift, later=None):
    names = ("APPS", "LAUNCHES", "PARALLEL", "SPINNERS", "TIMESHIFT", "TIMESHIFT_START", "TIMESHIFT_LATER",
             "EXTRA_ENV", "HTTPS")
    saved = {n: getattr(kvm, n) for n in names}
    kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = APPS, LAUNCHES, [], []
    kvm.TIMESHIFT, kvm.TIMESHIFT_START, kvm.TIMESHIFT_LATER = shift is not None, shift, later
    kvm.EXTRA_ENV = env
    kvm.HTTPS = [(PORTS[name], *certs[name]) for name in PORTS]
    try:
        return kvm.drive(d, bus)
    finally:
        for n, v in saved.items():
            setattr(kvm, n, v)


def drive(d, bus):
    folder = Path(tempfile.mkdtemp(prefix="karotzvm-https-"))
    record = []
    try:
        certs, ca_dir = make_certificates(folder)
        env = {"SSL_CERT_DIR": str(ca_dir)}
        record.append("== run checks")
        record.extend(run(d, bus, certs, env, None))
        record.append("== run clock")
        record.extend(run(d, bus, certs, env, BACK_TO_1970, (CLOCK_SET_S, 0)))
        record.append("== run unset")
        record.extend(run(d, bus, certs, env, BACK_TO_1970))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return record


def results(record):
    """{run: {request: result}} from the application's log."""
    out, current = {}, None
    for line in record:
        m = re.match(r"== run (\w+)$", line)
        if m:
            current = out.setdefault(m.group(1), {})
            continue
        m = re.search(r"JSLOG: (\w+) -> (.*)$", line)
        if m and current is not None:
            current[m.group(1)] = m.group(2)
    return out


def check(record):
    text = "\n".join(record)
    problems = []
    got = results(record)
    for name, want in EXPECTED.items():
        for request, result in want.items():
            have = got.get(name, {}).get(request)
            if have != result:
                problems.append(f"{name}: {request} gave {have or 'nothing'}, expected {result}")
    runs = re.split(r"^== run (\w+)$", text, flags=re.M)
    logs = dict(zip(runs[1::2], runs[2::2]))
    for name, (waits, gave_up) in WAITED.items():
        log = logs.get(name, "")
        if (WAITS in log) != waits:
            problems.append(f"{name}: the first HTTPS request {'did not wait' if waits else 'waited'} for the clock")
        if (GAVE_UP in log) != gave_up:
            problems.append(f"{name}: the wait for the clock {'did not run out' if gave_up else 'ran out'}")
    if text.count("JSLOG: https done") != 3:
        problems.append(f"the application finished {text.count('JSLOG: https done')} of 3 runs")
    return problems


def summary(record):
    got = results(record)
    return "; ".join(f"{name}: " + ", ".join(f"{r} {'ok' if v == HELLO else 'refused'}" for r, v in sorted(rs.items()))
                     for name, rs in got.items())
