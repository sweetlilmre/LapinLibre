#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A local platform server, for a Karotz on your own network.

The rabbit's controller connects to its platform (ip_server:port_server
in /usr/etc/conf/karotz.conf), logs in, and takes its configuration from it:
the applications to install (zip archives fetched over HTTP from url_app),
their instances, the schedules and the language. This server plays that
platform:

- it logs the controller in, whatever its id and password (account.conf on the
  rabbit must still have both, or the controller doesn't answer);
- it pushes a configuration made from an applications folder, one
  subfolder per application (descriptor.xml, main.js, ...), and serves each
  one as a zip at <url_app>/file/<apiKey>/<version>/get;
- it pushes the schedules from the settings file, answers the controller's
  pings and logs every message;
- on request (appctl.py, over a local control port) it starts a hosted
  application, takes interactive mode and sends commands (ears, LED, speech,
  sound), or sends any message;
- with --tts-port, it also runs tts_server.py's speech server, which
  voice-daemon uses for text to speech (a separate service on the rabbit,
  configured in voice.conf, not part of the platform protocol).

Messages are PlatformMessage protobufs (proto/platform-message.proto), each preceded by its
length (4 bytes, network order); they are encoded and decoded with protoc.

    python3 app_server.py --apps APPS_DIR [--settings settings.json]
                           [--listen 0.0.0.0] [--port 9123] [--http-port 8080]
                           [--control-port 9124] [--tts-port 8081]
                           [--codec auto|python|protoc] [--protoc protoc] [--proto PATH]

Python 3.10+. Messages go through the protobuf package and the generated
platform_message_pb2.py when they are installed (uv sync in this folder, see
pyproject.toml), otherwise through the protoc command; the standard library
is enough with protoc.
Only point a rabbit you own at it: the platform can install applications,
change the configuration and reboot the rabbit.
"""

import argparse
import datetime
import email
import email.policy
import io
import json
import re
import secrets
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PROTO = HERE.parent.parent / "userland" / "proto" / "platform-message.proto"
TYPE = "lapinlibre.platform.PlatformMessage"

# How long after `run` the application starts: `run` pushes a one-shot date
# schedule this far ahead (see Server.run_app()).
RUN_DELAY_S = 3


def zone_time(t, zone):
    """t (seconds since the epoch) as a datetime in the time zone `zone`, and
    where the zone's rules came from. Where Python has no time-zone database
    (Windows without the tzdata package) this machine's local time stands in,
    which is right when the rabbit is in the same zone as this machine."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.fromtimestamp(t, ZoneInfo(zone)), "time-zone database"
    except (ImportError, KeyError, ValueError):  # ZoneInfoNotFoundError is a KeyError
        return datetime.datetime.fromtimestamp(t), "this machine's local time"


def log(text):
    print(time.strftime("%H:%M:%S ") + text, flush=True)


def del_old(d, keep=200):
    """Drop a dict's oldest entries beyond `keep` (replies nobody waited for)."""
    for key in list(d)[:-keep]:
        del d[key]


def quote(value):
    """A protobuf text-format string literal."""
    return json.dumps(str(value))


SOCIAL_KEYS = ("twitterConsumerKey", "twitterConsumerSecret", "twitterToken", "twitterTokenSecret",
               "facebookToken", "soundcloudToken")


def contact_fields(c):
    """A Contact's fields in text format, from a settings entry
    ({"firstname", "lastname", "alias", "uuid", "objects": [{"idDB", "name"}]})."""
    fields = [f"{key}: {quote(c[key])}" for key in ("firstname", "lastname") if c.get(key)]
    for o in c.get("objects", []):
        fields.append("objects { idDB: " + quote(o["idDB"]) + (f" name: {quote(o['name'])}" if o.get("name") else "") + " }")
    fields += [f"{key}: {quote(c[key])}" for key in ("alias", "uuid") if c.get(key)]
    return " ".join(fields)


class MessageStore:
    """The Karotz messages the platform keeps for the rabbit, from the
    settings' "messages" ({"id", "from" (a contact), "date" (seconds since
    the epoch), "status" READ or UNREAD, "type" TEXT/SOUND/XML/MIXED,
    "content", "header"}). Applications reach them with
    karotz.message.list/get/delete/markAsRead, which the controller forwards
    here; each query is answered with a karotzMessageResponse. A message an
    application sends (action NEW) is added, from this rabbit."""

    def __init__(self, settings):
        self.me = settings.get("rabbit", {"firstname": "my", "lastname": "rabbit", "uuid": "rabbit"})
        self.messages = [dict(m) for m in settings.get("messages", [])]
        self.lock = threading.Lock()

    def unread(self):
        with self.lock:
            return any(m.get("status", "UNREAD") == "UNREAD" for m in self.messages)

    @staticmethod
    def _text(m, with_content):
        header = [f"id: {quote(m['id'])}", f"date: {int(m.get('date', 0))}", f"status: {m.get('status', 'UNREAD')}"]
        if m.get("from"):
            header.append("from { " + contact_fields(m["from"]) + " }")
        header.append("to { " + contact_fields(m.get("to") or {"uuid": "rabbit"}) + " }")
        text = "header { " + " ".join(header) + " }"
        if with_content:
            content = [f"type: {m.get('type', 'TEXT')}"]
            if m.get("header"):
                content.append(f"header: {quote(m['header'])}")
            content.append(f"content: {quote(m.get('content', ''))}")
            text += " content { " + " ".join(content) + " }"
        return "karotzMessageList { " + text + " }"

    def answer(self, action, ids, new_content=None):
        """The karotzMessageResponse body for a query, and a log line."""
        with self.lock:
            if action == "LIST":
                chosen, with_content = list(self.messages), False
            elif action == "GET":
                chosen, with_content = [m for m in self.messages if m["id"] in ids], True
            elif action == "DELETE":
                chosen = [m for m in self.messages if m["id"] in ids]
                self.messages = [m for m in self.messages if m["id"] not in ids]
                with_content = False
            elif action == "READ":
                chosen = [m for m in self.messages if m["id"] in ids]
                for m in chosen:
                    m["status"] = "READ"
                with_content = False
            elif action == "NEW":
                m = {"id": str(uuid.uuid4()), "from": self.me, "date": int(time.time()), "status": "UNREAD",
                     "type": "TEXT", "content": new_content or ""}
                self.messages.append(m)
                chosen, with_content = [m], False
            else:
                chosen, with_content = [], False
            body = "karotzMessageResponse { " + " ".join(self._text(m, with_content) for m in chosen) + " }"
        return body, f"{action} {', '.join(m['id'] for m in chosen) or '(none)'}"


def python_protobuf():
    """The protobuf package and the generated platform_message_pb2 module, or None."""
    sys.path.insert(0, str(HERE))
    try:
        from google.protobuf import text_format
        import platform_message_pb2
    except ImportError:
        return None
    finally:
        sys.path.remove(str(HERE))
    return text_format, platform_message_pb2.PlatformMessage


class Codec:
    """PlatformMessage <-> protobuf text format, with the protobuf package (pip, see
    pyproject.toml) or, failing that, the protoc command."""

    def __init__(self, protoc, proto, backend="auto"):
        self.protoc, self.proto = protoc, Path(proto).resolve()
        self.python = python_protobuf() if backend in ("auto", "python") else None
        if backend == "python" and self.python is None:
            sys.exit("app_server: --codec python needs the protobuf package (uv sync in tools/app-server, then generate platform_message_pb2.py, see README.md)")
        self.name = "protobuf package" if self.python else f"protoc ({protoc})"

    def _run(self, mode, data):
        r = subprocess.run([self.protoc, f"--{mode}={TYPE}", f"--proto_path={self.proto.parent}", str(self.proto)],
                           input=data, capture_output=True, timeout=30)
        if r.returncode != 0:
            raise ValueError(r.stderr.decode(errors="replace").strip())
        return r.stdout

    def encode(self, text, partial=False):
        """The message's bytes; with partial, required fields may be missing (protoc encodes such messages anyway)."""
        if self.python:
            text_format, msg_type = self.python
            msg = msg_type()
            try:
                text_format.Parse(text, msg)
                return msg.SerializePartialToString() if partial else msg.SerializeToString()
            except Exception as e:  # ParseError, EncodeError (missing required fields)
                raise ValueError(str(e)) from e
        return self._run("encode", text.encode())

    def decode(self, payload):
        if self.python:
            text_format, msg_type = self.python
            msg = msg_type()
            try:
                msg.MergeFromString(payload)  # like protoc --decode, tolerant of missing required fields
            except Exception as e:  # DecodeError
                raise ValueError(str(e)) from e
            # The same multi-line layout as protoc --decode (handle() parses it by line).
            return text_format.MessageToString(msg)
        return self._run("decode", payload).decode(errors="replace")


# ---- applications ---------------------------------------------------------------

class Apps:
    """The applications folder plus the settings file's instances and schedules."""

    def __init__(self, folder, settings_path=None):
        self.folder, self.settings_path = Path(folder), settings_path
        self.reload()

    def reload(self):
        """(Re)read the settings file."""
        self.settings = json.loads(Path(self.settings_path).read_text()) if self.settings_path else {}

    def descriptor(self, key):
        path = self.folder / key / "descriptor.xml"
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as e:
            log(f"apps: {key}: unreadable descriptor.xml ({e}), skipped")
            return None
        return {"version": (root.findtext("version") or "1").strip(),
                "name": (root.findtext("name") or key).strip(),
                "accesses": [a.text.strip() for a in root.iter("access") if a.text],
                # "external" applications use the REST API (rest_api.py) and
                # get their events at <callback>.
                "deployment": (root.findtext("deployment") or "hosted").strip(),
                "callback": (root.findtext("callback") or "").strip()}

    def instance_uuid(self, key, instance):
        return instance.get("uuid") or str(uuid.uuid5(uuid.NAMESPACE_URL, key + "/" + instance["name"]))

    def instance_uuids(self, key):
        return [self.instance_uuid(key, i) for i in self.instances(key)]

    def keys(self):
        return sorted(p.name for p in self.folder.iterdir() if (p / "descriptor.xml").is_file())

    def grammar_text(self):
        """The controller's launch grammar (ABNF): one alternative per instance of each hosted
        application, its words the instance's "voice" setting, else the application's name (and the
        instance's for one that isn't the default), its semantic the application and instance the
        controller starts (<app>key</app><par>instance</par>)."""
        alternatives = []
        for key in self.keys():
            d = self.descriptor(key)
            if d is None or d["deployment"] != "hosted":
                continue
            for instance in self.instances(key):
                name = instance["name"]
                title = d["name"]
                words = instance.get("voice") or (title if name == "default" else f"{title} {name}")
                words = " ".join(re.sub(r"[^\w' -]", " ", words).split())
                alternatives.append(f"{words} {{<app>{key}</app><par>{name}</par>}}")
        lang = self.settings.get("lang", "en-US")
        return (f"#ABNF 1.0 UTF-8;\nlanguage {lang};\nmode voice;\nroot $App;\n"
                f"public $App = {' | '.join(alternatives) or '$VOID'};\n")

    def instances(self, key):
        configured = self.settings.get("apps", {}).get(key, {}).get("instances")
        return configured or [{"name": "default"}]

    def zip(self, key):
        buffer = io.BytesIO()
        root = self.folder / key
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.name != "config.bin":
                    z.write(path, path.relative_to(root).as_posix())
        return buffer.getvalue()

    def config_text(self):
        parts = []
        for key in self.keys():
            d = self.descriptor(key)
            if d is None:
                continue
            app = [f"apiKey: {quote(key)}", f"version: {quote(d['version'])}", f"name: {quote(d['name'])}",
                   f"deployment: {quote(d['deployment'])}"]
            app += [f"access: {quote(a)}" for a in d["accesses"]]
            for instance in self.instances(key):
                fields = [f"name: {quote(instance['name'])}",
                          f"uuid: {quote(self.instance_uuid(key, instance))}"]
                if instance.get("rfid"):
                    # rfid-daemon reports the UID in upper-case hex, and the
                    # controller compares it case-sensitively.
                    fields.append(f"rfid: {quote(instance['rfid'].upper())}")
                for k, v in instance.get("params", {}).items():
                    fields.append(f"params {{ key: {quote(k)} value: {quote(v)} }}")
                app.append("configInstance { " + " ".join(fields) + " }")
            parts.append("configapp { " + " ".join(app) + " }")
        if self.settings.get("lang"):
            parts.append(f"lang: {quote(self.settings['lang'])}")
        parts.append(f"playsounds: {'true' if self.settings.get('playsounds', True) else 'false'}")
        social = self.settings.get("social")
        if social:
            # What social.init gives an application whose descriptor grants
            # the access (twitter, facebook, soundcloud).
            fields = [f"{key}: {quote(social[key])}" for key in SOCIAL_KEYS if social.get(key)]
            parts.append("social { " + " ".join(fields) + " }")
        return "config { " + " ".join(parts) + " }"

    def contacts_text(self):
        """contactList from the settings' contacts, or None. The controller
        keeps it and answers applications' contact.get from it."""
        contacts = self.settings.get("contacts")
        if not contacts:
            return None
        return "contactList { " + " ".join("contacts { " + contact_fields(c) + " }" for c in contacts) + " }"

    def schedule_text(self, extra=()):
        """scheduleConfig: the settings' schedules plus `extra` (apiKey, instance, schedule) entries.

        Every scheduleConfig makes the controller replace all its schedules
        and apply the wake-up/sleep times, which can reset the rabbit (and
        stop a running application that isn't interactive yet), so the
        server sends one only when the schedules change."""
        parts = []
        for s in self.settings.get("schedules", []):
            if "weekly" in s:
                w = s["weekly"]
                when = (f"scheduleWeekly {{ dayWeek: {w['day']} hour: {w['hour']} minute: {w['minute']} }}")
            elif "every" in s:
                when = f"schedulePeriodic {{ period: {int(s['every'])} }}"
            else:
                continue
            parts.append(f"scheduleApp {{ apiKey: {quote(s['app'])} instance: {quote(s.get('instance', 'default'))} "
                         f"schedule {{ {when} }} }}")
        for key, instance, schedule in extra:
            parts.append(f"scheduleApp {{ apiKey: {quote(key)} instance: {quote(instance)} "
                         f"schedule {{ {schedule} }} }}")
        for w in self.settings.get("wakeup", []):
            parts.append(f"scheduleWakeup {{ dayweek: {w['day']} wakeuphour: {w['wake'][0]} "
                         f"wakeupminute: {w['wake'][1]} sleephour: {w['sleep'][0]} sleepminute: {w['sleep'][1]} }}")
        if self.settings.get("timezone"):
            # The controller sets the rabbit's clock from `time` and its time
            # zone from `gmt` (looked up by name in its /etc/timezone.list).
            parts.append(f"time: {int(time.time() * 1000)} gmt: {quote(self.settings['timezone'])}")
        return "scheduleConfig { " + " ".join(parts) + " }"


class AppReports:
    """What applications report to /log, kept for appctl.py app-log (the last 1000)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lines = []
        self.count = 0

    def add(self, text):
        log(f"app: {text}")
        with self.lock:
            self.lines = (self.lines + [text])[-1000:]
            self.count += 1

    def since(self, count):
        with self.lock:
            new = min(self.count - count, len(self.lines))
            return {"count": self.count, "lines": self.lines[len(self.lines) - new:] if new > 0 else []}


APP_REPORTS = AppReports()


class HttpHandler(BaseHTTPRequestHandler):
    """Application downloads (/file/<apiKey>/<version>/get) for the
    controller, and for applications: /text (a page for http.get), /log?msg=
    (an application's report, printed on this console) and /upload (POST:
    what is posted is saved in the uploads folder: webcam.photo(url)'s
    picture, a recording sent with http.post, a form)."""
    apps = None
    uploads = None
    rest = None                 # rest_api.RestApi: /api/karotz/<action>

    def reply(self, code, body, ctype="text/plain; charset=UTF-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path.startswith("/api/karotz/") and self.rest is not None:
            code, xml = self.rest.handle(path[len("/api/karotz/"):], query)
            log(f"rest: {path} -> {code}")
            self.reply(code, xml, "text/xml; charset=UTF-8")
            return
        if re.fullmatch(r"/asr/grammar/[^/]+\.abnf", path):
            self.reply(200, self.apps.grammar_text(), "text/plain; charset=UTF-8")
            return
        if path == "/text":
            self.reply(200, "hello from the app server")
            return
        if path == "/log":
            params = urllib.parse.parse_qs(query)
            APP_REPORTS.add(" ".join(params.get("msg", ["(no msg)"])))
            self.reply(200, "OK")
            return
        m = re.fullmatch(r"/file/([^/]+)/([^/]+)/get", self.path)
        key = m.group(1) if m else None
        if key is None or key not in self.apps.keys():
            log(f"http: {self.path}: not found")
            self.send_error(404)
            return
        data = self.apps.zip(key)
        log(f"http: sending {key} ({len(data)} bytes)")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.partition("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if path == "/log":
            APP_REPORTS.add(body.decode(errors="replace"))
            self.reply(200, "OK")
            return
        if not (path == "/upload" or path.startswith("/upload/")):
            log(f"http: POST {self.path}: not found")
            self.send_error(404)
            return
        saved = []
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", path[len("/upload"):].strip("/")) or "upload"
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            # One file per part with a file name; the other fields are logged.
            msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body,
                                           policy=email.policy.HTTP)
            for part in msg.iter_parts():
                field = part.get_param("name", header="content-disposition") or "field"
                data = part.get_payload(decode=True) or b""
                if part.get_filename():
                    target = self.uploads / f"{stamp}-{name}-{re.sub(r'[^A-Za-z0-9_.-]', '_', part.get_filename())}"
                    target.write_bytes(data)
                    saved.append(f"{field}={target.name} ({len(data)} bytes)")
                else:
                    saved.append(f"{field}={data[:60].decode(errors='replace')!r}")
        else:
            target = self.uploads / f"{stamp}-{name}.bin"
            target.write_bytes(body)
            saved.append(f"{target.name} ({len(body)} bytes, {ctype or 'no type'})")
        log(f"http: upload {path}: {', '.join(saved) or 'nothing'}")
        self.reply(200, "OK")

    def log_message(self, format, *args):
        pass


# ---- the platform ----------------------------------------------------------------

class Platform:
    asr_answer = None           # asr_server.set_answer, with --asr-port

    def __init__(self, codec, apps, hold=False):
        self.codec, self.apps = codec, apps
        self.hold = hold            # wait for the "login" command before logging the controller in
        self.lock = threading.Condition()
        self.sock = None            # the controller's connection
        self.logged_in = False
        self.interactive_id = None
        self.replies = {}           # correlationId -> decoded reply, on one line
        self.reply_texts = {}       # correlationId -> decoded reply, as decoded (for the REST API)
        self.rest = None            # rest_api.RestApi, which posts events to external applications
        self.events = []            # recent messages from the controller
        self.event_count = 0        # messages received so far (the shell asks for the ones after N)
        self.store = MessageStore(apps.settings)

    @staticmethod
    def new_id():
        return str(uuid.uuid4())

    def send(self, text, msg_id=None, with_id=True, partial=False, cut=0):
        """Send a PlatformMessage (text format) under a new id; returns the id. For
        tests of malformed input: with_id=False leaves the id out, partial
        allows missing required fields, cut drops bytes from the end."""
        msg_id = (msg_id or self.new_id()) if with_id else None
        payload = self.codec.encode(f"id: {quote(msg_id)} {text}" if with_id else text, partial=partial or not with_id)
        payload = payload[:len(payload) - cut] if cut > 0 else payload
        with self.lock:
            if self.sock is None:
                raise ConnectionError("no controller connected")
            self.sock.sendall(struct.pack(">I", len(payload)) + payload)
        log(f"-> id: {quote(msg_id)} {text}" if msg_id else f"-> {text}")
        return msg_id

    def send_interactive(self, body):
        if self.interactive_id is None:
            raise RuntimeError("not in interactive mode (use: appctl.py take)")
        return self.send(f"interactiveId: {quote(self.interactive_id)} {body}")

    def wait_reply_text(self, msg_id, timeout=10):
        """The reply to msg_id as decoded (one field per line), or None."""
        deadline = time.time() + timeout
        with self.lock:
            while msg_id not in self.reply_texts and time.time() < deadline:
                self.lock.wait(deadline - time.time())
            self.replies.pop(msg_id, None)
            return self.reply_texts.pop(msg_id, None)

    def wait_reply(self, msg_id, timeout=10):
        deadline = time.time() + timeout
        with self.lock:
            while msg_id not in self.replies and time.time() < deadline:
                self.lock.wait(deadline - time.time())
            return self.replies.pop(msg_id, None)

    def push_configuration(self):
        self.send(self.apps.config_text())
        self.send(self.apps.schedule_text())
        contacts = self.apps.contacts_text()
        if contacts:
            self.send(contacts)
        self.send_pending()

    def send_pending(self):
        """The pending-message flag: the controller pulses the LED green and
        blue while it is set (green alone otherwise)."""
        self.send(f"pendingmessage: {'true' if self.store.unread() else 'false'}")

    def answer_message_query(self, text, msg_id):
        """A karotzMessageQuery an application sent (the controller forwards
        it here); the controller passes the answer back to the application."""
        block = text.split("karotzMessageQuery {", 1)[1]
        action = re.search(r"^\s+action: (\w+)", block, re.M)
        ids = re.findall(r'^\s{2}id: "([^"]*)"', block, re.M)
        content = re.search(r'^\s+content: "((?:[^"\\]|\\.)*)"', block, re.M)
        body, what = self.store.answer(action.group(1) if action else "?", ids,
                                       json.loads(f'"{content.group(1)}"') if content else None)
        log(f"messages: {what}")
        self.send(f"correlationId: {quote(msg_id)} response {{ code: OK }} {body}")
        if action and action.group(1) in ("DELETE", "READ", "NEW"):
            self.send_pending()

    def serve(self, sock, address):
        log(f"controller connected from {address[0]}:{address[1]}")
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self.sock, self.logged_in, self.interactive_id = sock, False, None
        if not self.hold:
            self.log_in()
        try:
            while True:
                head = self.recv_exact(sock, 4)
                text = self.codec.decode(self.recv_exact(sock, struct.unpack(">I", head)[0]))
                self.handle(text)
        except (OSError, EOFError, ValueError) as e:
            log(f"controller connection closed ({e or 'end of stream'})")
        finally:
            with self.lock:
                if self.sock is sock:
                    self.sock, self.logged_in, self.interactive_id = None, False, None

    def log_in(self):
        self.send(f"authenticationToken {{ token: {quote(secrets.token_hex(16))} }}")

    @staticmethod
    def recv_exact(sock, n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def handle(self, text):
        one_line = " ".join(text.split())
        m = re.search(r'^correlationId: "([^"]*)"', text, re.M)
        msg_id = re.search(r'^id: "([^"]*)"', text, re.M)
        top = set(re.findall(r"^(\w+) \{", text, re.M))
        if "ping" in top and m is None and "interactiveId" not in text:
            # Any message counts as the platform being alive.
            self.send(f"correlationId: {quote(msg_id.group(1) if msg_id else '')} response {{ code: OK }}")
            return
        log(f"<- {one_line}")
        with self.lock:
            if m:
                self.replies[m.group(1)] = one_line
                self.reply_texts[m.group(1)] = text
                del_old(self.reply_texts)
            self.events.append(one_line)
            self.event_count += 1
            del self.events[:-200]
            self.lock.notify_all()
        if self.rest is not None:
            self.rest.forward(text)
        if "karotzMessageQuery" in top and msg_id:
            self.answer_message_query(text, msg_id.group(1))
        if "authenticationResponse" in top and not self.logged_in:
            login = re.search(r'login: "([^"]*)"', text)
            log(f"logged in: {login.group(1) if login else '?'}")
            self.logged_in = True
            self.push_configuration()
        if "interactiveMode" in top and "action: STOP" in text:
            self.interactive_id = None

    # ---- control commands (appctl.py) ----

    def run_app(self, key, instance=None):
        if key not in self.apps.keys():
            raise ValueError(f"no application {key!r} in the applications folder")
        names = [i.get("name", "default") for i in self.apps.instances(key)]
        if instance is None:
            instance = names[0]  # the first configuration in the settings file
        elif instance not in names:
            raise ValueError(f"{key} has no instance {instance!r} (its instances: {', '.join(names)})")
        zone = self.apps.settings.get("timezone")
        if not zone:
            raise ValueError('run needs "timezone" in the settings: the rabbit\'s time zone, by the name '
                             'its /etc/timezone.list uses (for example "Europe/Paris")')
        # The platform has no "start this application" message, so this is a
        # one-shot date schedule RUN_DELAY_S ahead, in the rabbit's local time
        # (the same scheduleConfig sets its clock and zone). scheduler-daemon
        # drops a fully specified date entry once it has fired, so nothing
        # has to be removed afterwards: a second scheduleConfig would arrive
        # while the application is starting, and could reset the rabbit.
        when, source = zone_time(time.time() + RUN_DELAY_S, zone)
        date = (f"scheduleDate {{ year: {when.year} month: {when.month} day: {when.day} "
                f"hour: {when.hour} minute: {when.minute} second: {when.second} }}")
        self.send(self.apps.schedule_text([(key, instance, date)]))
        log(f"run: {key} ({instance}) at {when:%Y-%m-%d %H:%M:%S} {zone} ({source})")
        return f"{key} ({instance}) starts in about {RUN_DELAY_S} s"

    def command(self, cmd, args):
        if cmd == "status":
            return {"connected": self.sock is not None, "logged_in": self.logged_in,
                    "interactive_id": self.interactive_id, "apps": self.apps.keys()}
        if cmd == "events":
            with self.lock:
                return self.events[-int(args[0]) if args else -20:]
        if cmd == "events-since":
            # For appctl.py shell: the messages after the first N, and the new N.
            with self.lock:
                since = int(args[0]) if args else self.event_count
                new = min(self.event_count - since, len(self.events))
                return {"count": self.event_count, "events": self.events[len(self.events) - new:] if new > 0 else []}
        if cmd == "app-log":
            # What applications reported to /log after the first N reports (all kept ones without N).
            return APP_REPORTS.since(int(args[0]) if args else 0)
        if cmd == "info":
            # The rabbit's information (controller: karotz_info.cc).
            return self.wait_reply(self.send("karotzinfo_query { }")) or "no answer"
        if cmd == "system":
            names = ("TELNET_ON", "TELNET_OFF", "LOG_ON", "LOG_OFF", "LOG_REMOTE_ON", "LOG_REMOTE_OFF", "SYSTEM_REBOOT")
            if not args or args[0].upper() not in names:
                raise ValueError(f"system needs one of {', '.join(names)}")
            self.send(f"system {{ command: {args[0].upper()} }}")
            return f"{args[0].upper()} sent (the controller doesn't answer it)"
        if cmd == "pending":
            # Set the pending-message flag by hand (on/off), or from the store.
            if args:
                self.send(f"pendingmessage: {'true' if args[0] in ('on', 'true', '1') else 'false'}")
            else:
                self.send_pending()
            return "pending-message flag sent"
        if cmd == "messages":
            def name(c):
                return " ".join(c.get(k) for k in ("firstname", "lastname") if c.get(k)) or c.get("alias") or "?"
            with self.store.lock:
                return [f"{m['id']} {m.get('status', 'UNREAD')} from {name(m.get('from') or {})}: "
                        f"{m.get('content', '')}" for m in self.store.messages]
        if cmd == "asr-answer":
            # What the speech-recognition server (--asr-port) answers from now on.
            if self.asr_answer is None:
                raise ValueError("no speech-recognition server (start the server with --asr-port)")
            answer = " ".join(args) or "first"
            self.asr_answer(answer)
            return f"speech recognition now answers {answer!r}"
        if cmd == "contacts":
            contacts = self.apps.contacts_text()
            if contacts is None:
                return "no contacts in the settings"
            self.send(contacts)
            return "contact list sent"
        if cmd == "login":
            self.hold = False
            self.log_in()
            return "authentication token sent"
        if cmd == "reload":
            self.apps.reload()
            self.store = MessageStore(self.apps.settings)
            self.push_configuration()
            return "configuration pushed"
        if cmd == "run":
            return self.run_app(*args[:2])
        if cmd == "take":
            ttl = int(args[0]) if args else 900
            new_id = self.new_id()
            msg = self.send(f"interactiveMode {{ action: START interactiveId: {quote(new_id)} ttl: {ttl} }}")
            reply = self.wait_reply(msg)
            if reply and "code: OK" in reply:
                self.interactive_id = new_id
            return reply or "no answer"
        if cmd == "take-as":
            # Interactive mode under a chosen id ("" included, which the
            # controller refuses with BAD_INTERACTIVEID).
            if not args:
                raise ValueError("take-as needs an interactive id (\"\" for the empty one)")
            ttl = int(args[1]) if len(args) > 1 else 900
            msg = self.send(f"interactiveMode {{ action: START interactiveId: {quote(args[0])} ttl: {ttl} }}")
            reply = self.wait_reply(msg)
            if reply and "code: OK" in reply:
                self.interactive_id = args[0]
            return reply or "no answer"
        if cmd == "send-raw":
            # A message as given, for tests of malformed input: --no-id leaves
            # out the (required) id, --cut N drops N bytes from its end, and
            # required fields may be missing.
            with_id, cut, rest = True, 0, list(args)
            while rest and rest[0].startswith("--"):
                flag = rest.pop(0)
                if flag == "--no-id":
                    with_id = False
                elif flag == "--cut" and rest:
                    cut = int(rest.pop(0))
                else:
                    raise ValueError(f"send-raw: unknown option {flag!r} (--no-id, --cut N)")
            msg = self.send(" ".join(rest), with_id=with_id, partial=True, cut=cut)
            if msg is None:
                time.sleep(2)
                return "sent without an id (an answer can't be matched to it; see events)"
            return self.wait_reply(msg, timeout=5) or "sent (no answer)"
        if cmd == "release":
            if self.interactive_id is None:
                return "not in interactive mode"
            msg = self.send_interactive("interactiveMode { action: STOP }")
            self.interactive_id = None
            return self.wait_reply(msg) or "no answer"
        bodies = {
            "ears": lambda a: f"ears {{ left: {int(a[0])} right: {int(a[1])} }}",
            "ears-reset": lambda a: "ears { reset: true }",
            "led": lambda a: f"led {{ light {{ color: {quote(a[0])} }} }}",
            "pulse": lambda a: f"led {{ pulse {{ color: {quote(a[0])} period: {int(a[1]) if len(a) > 1 else 500} "
                               f"pulse: {int(a[2]) if len(a) > 2 else -1} }} }}",
            "fade": lambda a: f"led {{ fade {{ color: {quote(a[0])} period: {int(a[1]) if len(a) > 1 else 1000} }} }}",
            "say": lambda a: f"tts {{ action: START text: {quote(' '.join(a))} }}",
            "say-in": lambda a: f"tts {{ action: START lang: {quote(a[0])} text: {quote(' '.join(a[1:]))} }}",
            "say-stop": lambda a: "tts { action: STOP }",
            # Commands with one free-text argument take all their words, so
            # "play title::Little Wing" needs no quotes (in the shell either).
            "play": lambda a: f"multimedia {{ play {{ url: {quote(' '.join(a))} }} }}",
            "stop": lambda a: "multimedia { stop { } }",
            "pause": lambda a: "multimedia { pause { } }",
            "resume": lambda a: "multimedia { resume { } }",
            "next": lambda a: "multimedia { next { } }",
            "previous": lambda a: "multimedia { previous { } }",
            # The library lists (MPD, from a USB key): one entry, the names joined by ':'.
            "artists": lambda a: "multimedia { getartist { } }",
            "folders": lambda a: "multimedia { getfolder { } }",
            "genres": lambda a: "multimedia { getgenre { } }",
            "playlists": lambda a: "multimedia { getplaylist { } }",
            "songs": lambda a: "multimedia { getsong { } }",
            # A recording to /tmp/record.flac on the rabbit; the URL is carried, not used.
            "record": lambda a: f"multimedia {{ record {{ url: {quote(' '.join(a) or 'record')} }} }}",
            # A picture, which webcam-daemon POSTs to the URL (e.g. this server's /upload).
            "photo": lambda a: f"webcam {{ photo: {quote(' '.join(a))} }}",
            # Speech recognition of one of the given words (needs voice.conf's [asr] server).
            "listen": lambda a: "asr { " + " ".join(f"dictionary: {quote(w)}" for w in a[1:]) +
                                f" lang: {quote(a[0])} }}",
            "choreography": lambda a: f"choreography {{ start_file: {quote(' '.join(a))} }}",
            "ping": lambda a: "ping { }",
        }
        if cmd in bodies:
            return self.wait_reply(self.send_interactive(bodies[cmd](args))) or "no answer"
        if cmd == "send":
            text = " ".join(args)
            if self.interactive_id and "interactiveId" not in text:
                text = f"interactiveId: {quote(self.interactive_id)} {text}"
            return self.wait_reply(self.send(text), timeout=5) or "sent (no answer)"
        raise ValueError(f"unknown command {cmd!r}")


class ControlHandler(socketserver.StreamRequestHandler):
    platform = None

    def handle(self):
        for line in self.rfile:
            try:
                request = json.loads(line)
                answer = {"ok": True, "result": self.platform.command(request["cmd"], request.get("args", []))}
            except Exception as e:  # reported to appctl.py
                answer = {"ok": False, "error": str(e)}
            self.wfile.write((json.dumps(answer) + "\n").encode())


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apps", required=True, help="applications folder (one subfolder per application)")
    p.add_argument("--settings", help="settings file (JSON): instances, schedules, wake-up times, lang")
    p.add_argument("--listen", default="0.0.0.0", help="address for the controller and HTTP (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=9123)
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--control-port", type=int, default=9124, help="local control port for appctl.py (127.0.0.1)")
    p.add_argument("--control-socket", help="a Unix socket for control instead of the port")
    p.add_argument("--hold", action="store_true",
                   help="log the controller in only on `appctl.py login` (for tests)")
    p.add_argument("--asr-port", type=int,
                   help="also run the speech-recognition server (asr_server.py) on this port, e.g. 8443")
    p.add_argument("--asr-answer", default="first",
                   help='what it answers: "first" (the grammar\'s first alternative), "nomatch" or the words')
    p.add_argument("--uploads", default=str(HERE / "uploads"),
                   help="where POSTs to /upload are saved (default: uploads/ next to this script)")
    p.add_argument("--tts-port", type=int,
                   help="also run the speech server (tts_server.py) on this port, e.g. 8081")
    p.add_argument("--codec", choices=["auto", "python", "protoc"], default="auto",
                   help="encode/decode with the protobuf package or the protoc command (default: the package if installed)")
    p.add_argument("--protoc", default="protoc", help="the protoc command, when that is the codec")
    p.add_argument("--proto", default=str(DEFAULT_PROTO))
    a = p.parse_args()

    apps = Apps(a.apps, a.settings)
    codec = Codec(a.protoc, a.proto, a.codec)
    platform = Platform(codec, apps, hold=a.hold)
    log(f"applications: {', '.join(apps.keys()) or '(none)'}; messages through the {codec.name}")

    sys.path.insert(0, str(HERE))
    import rest_api
    platform.rest = HttpHandler.rest = rest_api.RestApi(platform, apps, log)
    HttpHandler.apps = apps
    HttpHandler.uploads = Path(a.uploads)
    HttpHandler.uploads.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((a.listen, a.http_port), HttpHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    ControlHandler.platform = platform
    if a.control_socket:
        control = socketserver.ThreadingUnixStreamServer(a.control_socket, ControlHandler)
    else:
        control = socketserver.ThreadingTCPServer(("127.0.0.1", a.control_port), ControlHandler)
    control.daemon_threads = True
    threading.Thread(target=control.serve_forever, daemon=True).start()

    speech = ""
    if a.tts_port:
        # A separate service on the rabbit (voice.conf), run here for convenience.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import tts_server
        try:
            tts = tts_server.speech_server(a.listen, a.tts_port)
        except (RuntimeError, OSError) as e:
            log(f"speech server not started: {e}")
        else:
            threading.Thread(target=tts.serve_forever, daemon=True).start()
            speech = f", speech on :{a.tts_port}"

    if a.asr_port:
        # Also a separate service on the rabbit (voice.conf [asr]).
        sys.path.insert(0, str(HERE))
        import asr_server
        asr = asr_server.asr_server(a.listen, a.asr_port, a.asr_answer, keep=a.uploads)
        threading.Thread(target=asr.serve_forever, daemon=True).start()
        Platform.asr_answer = staticmethod(asr_server.set_answer)
        # The controller names its launch grammar http://<server>/asr/grammar/<id>.abnf (port 80):
        # this server's own grammar answers for it.
        asr_server.set_grammar_source(
            lambda url: apps.grammar_text() if re.search(r"/asr/grammar/[^/]+\.abnf$", url) else None)
        speech += f", recognition on :{a.asr_port}"

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((a.listen, a.port))
    server.listen(5)
    where = a.control_socket or f"127.0.0.1:{a.control_port}"
    log(f"listening on {a.listen}:{a.port} (apps over HTTP on :{a.http_port}{speech}, control on {where})")
    try:
        while True:
            sock, address = server.accept()
            threading.Thread(target=platform.serve, args=(sock, address), daemon=True).start()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
