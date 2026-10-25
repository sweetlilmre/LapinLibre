# SPDX-License-Identifier: MIT
"""The REST API external applications used (api.karotz.com/api/karotz/...),
served by app_server.py on its HTTP port under /api/karotz/.

An "external" application (descriptor.xml <deployment>external</deployment>)
runs on a server of its own and drives the rabbit over HTTP:

  GET /api/karotz/start?apikey=..&installid=..&once=..&timestamp=..&signature=..
      takes interactive mode for the application's instance (installid is
      the instance's uuid, as configured); the answer is a VoosMsg in XML
      with interactiveMode { action START, interactiveId, configId, access }.
      The signature (base64 HMAC-SHA1 over the other parameters, sorted by
      name and joined with &, with the application's secret) is checked when
      the settings give the application a "secret", and not otherwise.
  GET /api/karotz/<action>?interactiveid=..&...  one command; the answer is
      the controller's reply in XML (<response><code>OK</code></response>):
      ears (left, right, relative, reset), led (action=light|pulse|fade,
      color, period, pulse), tts (action=speak|stop, text, lang), multimedia
      (action=play|pause|resume|stop|next|previous|record, url), webcam
      (action=photo|video, url), asr (grammar: words separated by |, lang),
      choreography (start, file, action=stop), interactivemode (action=stop).

What happens later for a request (its events, the rabbit's button, RFID
and ears callbacks while the application is interactive) is POSTed, as
VoosMsg XML, to the descriptor's <callback> URL, as the platform did.
"""

import base64
import hashlib
import hmac
import re
import threading
import urllib.parse
import urllib.request
import uuid
from xml.sax.saxutils import escape

COLOR = re.compile(r"^[0-9A-Fa-f]{6}$")


def text_to_xml(text):
    """protobuf text format (one field per line, as the codec decodes it) as XML."""
    out, stack = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith("{"):
            name = line[:-1].strip()
            stack.append(name)
            out.append(f"<{name}>")
        elif line == "}":
            out.append(f"</{stack.pop()}>")
        else:
            name, _, value = line.partition(":")
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = bytes(value[1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
            out.append(f"<{name.strip()}>{escape(value)}</{name.strip()}>")
    return "<VoosMsg>" + "".join(out) + "</VoosMsg>"


def signature_ok(params, secret):
    query = "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "signature")
    expected = base64.b64encode(hmac.new(secret.encode(), query.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, params.get("signature", ""))


def quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def flag(params, name):
    return params.get(name, "").lower() in ("1", "true", "yes")


def command_body(action, p):
    """The VoosMsg body for a REST command, or raise ValueError."""
    if action == "ears":
        if flag(p, "reset"):
            return "ears { reset: true }"
        parts = [f"{side}: {int(p[side])}" for side in ("left", "right") if side in p]
        if not parts:
            raise ValueError("ears needs left and/or right, or reset=true")
        return "ears { " + " ".join(parts) + (" relative: true" if flag(p, "relative") else "") + " }"
    if action == "led":
        kind, color = p.get("action", "light"), p.get("color", "")
        if not COLOR.match(color):
            raise ValueError("led needs color=RRGGBB")
        if kind == "light":
            return f"led {{ light {{ color: {quote(color)} }} }}"
        if kind == "fade":
            return f"led {{ fade {{ color: {quote(color)} period: {int(p.get('period', 1000))} }} }}"
        if kind == "pulse":
            return (f"led {{ pulse {{ color: {quote(color)} period: {int(p.get('period', 500))} "
                    f"pulse: {int(p.get('pulse', -1))} }} }}")
        raise ValueError("led action is light, fade or pulse")
    if action == "tts":
        if p.get("action", "speak") == "stop":
            return "tts { action: STOP }"
        lang = f" lang: {quote(p['lang'])}" if p.get("lang") else ""
        return f"tts {{ action: START text: {quote(p.get('text', ''))}{lang} }}"
    if action == "multimedia":
        kind = p.get("action", "play")
        if kind in ("play", "record"):
            return f"multimedia {{ {kind} {{ url: {quote(p.get('url', ''))} }} }}"
        if kind in ("pause", "resume", "stop", "next", "previous"):
            return f"multimedia {{ {kind} {{ }} }}"
        raise ValueError("multimedia action is play, pause, resume, stop, next, previous or record")
    if action == "webcam":
        kind = p.get("action", "photo")
        return f"webcam {{ photo: {quote('video' if kind == 'video' else p.get('url', ''))} }}"
    if action == "asr":
        words = [w.strip() for w in p.get("grammar", "").split("|") if w.strip()]
        if not words:
            raise ValueError("asr needs grammar=word|word...")
        lang = f" lang: {quote(p['lang'])}" if p.get("lang") else ""
        return "asr { " + " ".join(f"dictionary: {quote(w)}" for w in words) + lang + " }"
    if action == "choreography":
        if p.get("action") == "stop":
            return "choreography { stop: true }"
        if p.get("file"):
            return f"choreography {{ start_file: {quote(p['file'])} }}"
        return f"choreography {{ start: {quote(p.get('start', ''))} }}"
    raise ValueError(f"unknown action {action!r}")


class RestApi:
    def __init__(self, platform, apps, log):
        self.platform, self.apps, self.log = platform, apps, log
        self.sessions = {}      # interactiveId -> application key (sessions started over REST)
        self.requests = {}      # message id -> application key (for the events that follow)
        self.lock = threading.Lock()

    def error(self, status, text):
        return status, "<VoosMsg><response><code>ERROR</code><description>" + escape(text) + \
            "</description></response></VoosMsg>"

    def handle(self, action, query):
        """(HTTP status, XML) for GET /api/karotz/<action>?<query>."""
        p = {k: v[-1] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        try:
            if action == "start":
                return self.start(p)
            iid = p.get("interactiveid")
            if not iid:
                return self.error(400, "interactiveid missing")
            if action == "interactivemode":
                if p.get("action") != "stop":
                    return self.error(400, "interactivemode action is stop")
                body = "interactiveMode { action: STOP }"
            else:
                body = command_body(action, p)
        except (ValueError, KeyError) as e:
            return self.error(400, str(e))
        msg = self.platform.send(f"interactiveId: {quote(iid)} {body}")
        with self.lock:
            key = self.sessions.get(iid)
            if key:
                self.requests[msg] = key
        reply = self.platform.wait_reply_text(msg)
        if action == "interactivemode":
            with self.lock:
                self.sessions.pop(iid, None)
        if reply is None:
            return 504, "<VoosMsg><response><code>TIMEOUT</code></response></VoosMsg>"
        return 200, text_to_xml(reply)

    def start(self, p):
        key, installid = p.get("apikey", ""), p.get("installid", "")
        if key not in self.apps.keys():
            return self.error(403, f"unknown apikey {key!r}")
        secret = self.apps.settings.get("apps", {}).get(key, {}).get("secret")
        if secret and not signature_ok(p, secret):
            return self.error(403, "bad signature")
        configs = self.apps.instance_uuids(key)
        config = installid if installid in configs else (configs[0] if configs and not installid else None)
        if config is None:
            return self.error(403, f"no instance {installid!r} of {key}")
        iid = str(uuid.uuid4())
        accesses = (self.apps.descriptor(key) or {}).get("accesses", [])
        access = " ".join(f"access: {quote(a)}" for a in accesses)
        msg = self.platform.send(f"interactiveMode {{ action: START interactiveId: {quote(iid)} "
                                 f"configId: {quote(config)} ttl: 900 {access} }}")
        reply = self.platform.wait_reply_text(msg)
        if reply is None or "code: OK" not in reply:
            return 403, text_to_xml(reply) if reply else self.error(504, "no answer from the rabbit")[1]
        with self.lock:
            self.sessions[iid] = key
        self.platform.interactive_id = iid
        self.log(f"rest: {key} interactive as {iid}")
        acc = "".join(f"<access>{escape(a)}</access>" for a in accesses)
        return 200, (f"<VoosMsg><id>{uuid.uuid4()}</id><interactiveMode><action>START</action>"
                     f"<interactiveId>{iid}</interactiveId><configId>{escape(config)}</configId>{acc}"
                     f"</interactiveMode></VoosMsg>")

    def forward(self, text):
        """A message from the controller: POST it to the callback of the
        application it belongs to (a request it made, or its interactive id)."""
        corr = re.search(r'^correlationId: "([^"]*)"', text, re.M)
        iid = re.search(r'^interactiveId: "([^"]*)"', text, re.M)
        with self.lock:
            key = (self.requests.get(corr.group(1)) if corr else None) or \
                (self.sessions.get(iid.group(1)) if iid else None)
        if not key:
            return
        url = (self.apps.descriptor(key) or {}).get("callback")
        if not url:
            return
        data = text_to_xml(text).encode()

        def post():
            try:
                urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST",
                                                              headers={"Content-Type": "text/xml"}), timeout=10)
            except OSError as e:
                self.log(f"rest: callback {url} failed: {e}")
        threading.Thread(target=post, daemon=True).start()
