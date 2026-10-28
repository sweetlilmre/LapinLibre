# SPDX-License-Identifier: MIT
"""karotzVM: social.facebook and social.soundcloud on the URLs they allow.

social's get and post only take https://<host>.facebook.com/... and
https://<host>.soundcloud.com/... URLs. In the helper's namespaces a hosts
file puts graph.facebook.com and api.soundcloud.com on 127.0.0.1 (bound over
the sysroot's /etc/hosts there only), and the helper serves 127.0.0.1:8088's
pages over HTTPS on port 443 with a certificate for both names, signed by a
test CA karotzVM finds in $SSL_CERT_DIR. The tokens come from the helper's
answer to social.init (facebookToken "fb", soundcloudToken "sc").

What must hold: each call reaches the server with the account's token, get
as a query parameter (after "?" or, when the URL has a query, "&") and post
as a form field beside the application's own, and returns the response
body; the tokens never show in the application's log; a URL on another host
or over plain HTTP is still refused.

ARM only, like the karotzVM scenario.
"""

import re
import shutil
import tempfile
from pathlib import Path

from aggressive import karotzvm_https as kh
from scenarios import karotzVM as kvm

DAEMON = "karotzVM"
DEVICES = []

NAMES = ("graph.facebook.com", "api.soundcloud.com")
HELLO = '"hello from 127.0.0.1"'
REFUSED = "threw unauthorised url"

SOCIAL_JS = kvm.PRELUDE + r"""
add(function () {
    social.init(function () {
        attempt("fb get", function () { return social.facebook.get("https://graph.facebook.com/me"); });
        attempt("fb get query", function () { return social.facebook.get("https://graph.facebook.com/me?fields=id"); });
        attempt("fb post", function () { return social.facebook.post("https://graph.facebook.com/feed", {message: "hi"}); });
        attempt("sc get", function () { return social.soundcloud.get("https://api.soundcloud.com/me"); });
        attempt("sc post", function () { return social.soundcloud.post("https://api.soundcloud.com/tracks", {title: "t"}); });
        attempt("fb other host", function () { return social.facebook.get("https://graph.facebook.com.example/me"); });
        attempt("fb plain", function () { return social.facebook.get("http://graph.facebook.com/me"); });
        attempt("sc other host", function () { return social.soundcloud.get("https://graph.facebook.com/me"); });
        next();
    }, {d: 1});
});
add(function () { log("social done"); exit(); });
start();
"""

APPS = {"social": {"accesses": kvm.ALL_ACCESSES, "main": SOCIAL_JS}}
LAUNCHES = [("social manual", 180)]

# What each call must give, from the application's log.
EXPECTED = {
    "fb get": HELLO, "fb get query": HELLO, "fb post": HELLO, "sc get": HELLO, "sc post": HELLO,
    "fb other host": REFUSED, "fb plain": REFUSED, "sc other host": REFUSED,
}
# The requests the server must see (method, path, body), in any order.
REQUESTS = [
    ("GET", "/me?access_token=fb", "b''"),
    ("GET", "/me?fields=id&access_token=fb", "b''"),
    ("POST", "/feed", "message=hi&access_token=fb"),
    ("GET", "/me?oauth_token=sc", "b''"),
    ("POST", "/tracks", "title=t&oauth_token=sc"),
]


def make_certificate(folder):
    """(certificate, key) for NAMES signed by a test CA, and the CA's directory (named by its hash)."""
    ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    kh.openssl(folder, "req", "-x509", *ec, "-keyout", "ca.key", "-out", "ca.pem", "-days", "3650",
               "-subj", "/CN=LapinLibre test CA", "-addext", "basicConstraints=critical,CA:TRUE",
               "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    (folder / "names.ext").write_text("subjectAltName=" + ",".join(f"DNS:{n}" for n in NAMES) + "\n")
    kh.openssl(folder, "req", "-new", *ec, "-keyout", "social.key", "-out", "social.csr", "-subj", "/CN=social")
    kh.openssl(folder, "x509", "-req", "-in", "social.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
               "-CAcreateserial", "-days", "3650", "-extfile", "names.ext", "-out", "social.pem")
    ca_dir = folder / "ca"
    ca_dir.mkdir()
    h = kh.subprocess.run(["openssl", "x509", "-noout", "-subject_hash", "-in", str(folder / "ca.pem")],
                          check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    shutil.copy(folder / "ca.pem", ca_dir / f"{h}.0")
    return (folder / "social.pem", folder / "social.key"), ca_dir


def hosts_setup(d, folder):
    """Shell commands that bind a hosts file naming NAMES over the sysroot's /etc/hosts."""
    hosts = folder / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost " + " ".join(NAMES) + "\n")
    target = (Path(d.sysroot) / "etc" / "hosts").resolve()
    return f"mount --bind {hosts} {target}; "


def drive(d, bus):
    folder = Path(tempfile.mkdtemp(prefix="karotzvm-social-"))
    names = ("APPS", "LAUNCHES", "PARALLEL", "SPINNERS", "EXTRA_ENV", "HTTPS", "SANDBOX_SETUP")
    saved = {n: getattr(kvm, n) for n in names}
    try:
        (cert, key), ca_dir = make_certificate(folder)
        kvm.APPS, kvm.LAUNCHES, kvm.PARALLEL, kvm.SPINNERS = APPS, LAUNCHES, [], []
        kvm.EXTRA_ENV = {"SSL_CERT_DIR": str(ca_dir)}
        kvm.HTTPS = [(443, cert, key)]
        kvm.SANDBOX_SETUP = hosts_setup(d, folder)
        return kvm.drive(d, bus)
    finally:
        for n, v in saved.items():
            setattr(kvm, n, v)
        shutil.rmtree(folder, ignore_errors=True)


def results(record):
    """{call: result} from the application's log."""
    out = {}
    for line in record:
        m = re.search(r"JSLOG: (.+?) (-> (.*)|threw .*)$", line)
        if m:
            out[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return out


def requests(record):
    """[(method, path, body)] the server saw."""
    out = []
    for line in record:
        m = re.match(r"(GET|POST) (\S+) \[.*\] (.*)$", line)
        if m:
            body = m.group(3)
            if body.startswith(("b'", 'b"')) and body != "b''":
                body = body[2:-1]
            out.append((m.group(1), m.group(2), body))
    return out


def summary(record):
    return "; ".join(f"{k} {v}" for k, v in results(record).items())


def check(record):
    problems = []
    got = results(record)
    for call, want in EXPECTED.items():
        if got.get(call) != want:
            problems.append(f"{call} gave {got.get(call) or 'nothing'}, expected {want}")
    seen = requests(record)
    for want in REQUESTS:
        if want not in seen:
            problems.append(f"the server didn't see {want}: {seen}")
    if len(seen) != len(REQUESTS):
        problems.append(f"{len(seen)} requests, expected {len(REQUESTS)}: {seen}")
    logs = "\n".join(l for l in record if "JSLOG:" in l)
    if "access_token" in logs or "oauth_token" in logs:
        problems.append("a token shows in the application's log")
    if "social done" not in logs:
        problems.append("the application didn't finish")
    return problems
