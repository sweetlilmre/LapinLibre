# SPDX-License-Identifier: MIT
"""tools/app-server/app_server.py against a fake controller.

The test starts the server (protoc codec) on local ports and plays the
rabbit's controller: it connects, answers the login, and checks what the
server pushes and how it answers what the controller forwards from
applications. Also the server's HTTP endpoints for applications and its
control commands (appctl.py).

    python3 -I tests/app_server/test_app_server.py [PROTOC]

PROTOC defaults to the build's host protoc (scripts/common.sh's work
location) or `protoc` on PATH.
"""

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO / "tools" / "app-server"
sys.path.insert(0, str(SERVER_DIR))
import app_server  # noqa: E402
import appctl  # noqa: E402

PROTO = REPO / "userland" / "proto" / "platform-message.proto"

SETTINGS = {
    "apps": {"extapp": {"secret": "s3cret", "instances": [{"name": "default", "uuid": "inst-ext"}]},
             "pairapp": {"instances": [{"name": "kitchen"}, {"name": "hall"}]}},
    "timezone": "Europe/Paris",
    "lang": "fr-FR",
    "playsounds": False,
    "social": {"twitterConsumerKey": "ck", "twitterConsumerSecret": "cs", "facebookToken": "fb"},
    "contacts": [{"firstname": "Ann", "lastname": "Lee", "uuid": "u-ann", "objects": [{"idDB": "o1", "name": "rabbit"}]},
                 {"alias": "bob", "objects": [{"idDB": "o2"}]}],
    "messages": [{"id": "m1", "from": {"firstname": "Ann", "uuid": "u-ann"}, "date": 1700000000, "status": "UNREAD",
                  "type": "TEXT", "content": "hello there", "header": "subject"},
                 {"id": "m2", "from": {"firstname": "Bob"}, "date": 1700000100, "status": "READ",
                  "type": "TEXT", "content": "second"}],
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_protoc():
    if len(sys.argv) > 1:
        return sys.argv.pop(1)
    for candidate in (Path.home() / ".cache" / "lapinlibre" / "work" / "system" / "out" / "host" / "bin" / "protoc-2.3.0",):
        if candidate.exists():
            return str(candidate)
    return shutil.which("protoc") or "protoc"


PROTOC = find_protoc()


class FakeController:
    """The controller's side of the platform connection."""

    def __init__(self, port, codec):
        self.codec = codec
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.inbox = []

    def recv(self, timeout=10):
        self.sock.settimeout(timeout)
        head = self._exact(4)
        return self.codec.decode(self._exact(struct.unpack(">I", head)[0]))

    def _exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def send(self, text):
        payload = self.codec.encode(text)
        self.sock.sendall(struct.pack(">I", len(payload)) + payload)

    def raw_until(self, accept, timeout=10):
        """The first frame's bytes for which accept(payload) is true (others that decode are kept)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                head = self._exact(4)
            except socket.timeout:
                continue
            payload = self._exact(struct.unpack(">I", head)[0])
            if accept(payload):
                return payload
            try:
                self.inbox.append(self.codec.decode(payload))
            except ValueError:
                pass
        raise AssertionError("no matching frame")

    def until(self, pattern, timeout=10):
        """The first message received (now or kept from before) matching pattern."""
        deadline = time.time() + timeout
        while True:
            for i, text in enumerate(self.inbox):
                if re.search(pattern, text, re.S | re.M):
                    return self.inbox.pop(i)
            left = deadline - time.time()
            if left <= 0:
                raise AssertionError(f"no message matching {pattern!r}; got {self.inbox}")
            try:
                self.inbox.append(self.recv(timeout=left))
            except socket.timeout:
                pass

    def close(self):
        self.sock.close()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="app-server-test-"))
        cls.callbacks = []
        cls.receiver = CallbackReceiver(cls.callbacks)
        apps = cls.dir / "apps"
        shutil.copytree(SERVER_DIR / "example-apps", apps)
        # Hosted, with two instances and no speech grammar entry.
        (apps / "pairapp").mkdir()
        (apps / "pairapp" / "descriptor.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><descriptor><version>1</version><name>Pair</name>'
            '<accesses><access>led</access></accesses></descriptor>')
        (apps / "pairapp" / "main.js").write_text("exit();\n")
        (apps / "extapp").mkdir()
        (apps / "extapp" / "descriptor.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?><descriptor><version>1</version><name>External</name>'
            '<deployment>external</deployment>'
            f'<callback>http://127.0.0.1:{cls.receiver.port}/events</callback>'
            '<accesses><access>led</access><access>ears</access></accesses></descriptor>')
        (cls.dir / "settings.json").write_text(json.dumps(SETTINGS))
        cls.uploads = cls.dir / "uploads"
        cls.port, cls.http, cls.control, cls.asr = free_port(), free_port(), free_port(), free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", str(SERVER_DIR / "app_server.py"), "--apps", str(cls.dir / "apps"),
             "--settings", str(cls.dir / "settings.json"), "--listen", "127.0.0.1", "--port", str(cls.port),
             "--http-port", str(cls.http), "--control-port", str(cls.control), "--uploads", str(cls.uploads),
             "--asr-port", str(cls.asr),
             "--codec", "protoc", "--protoc", PROTOC, "--proto", str(PROTO)],
            stdout=open(cls.dir / "server.log", "w"), stderr=subprocess.STDOUT)
        cls.codec = app_server.Codec(PROTOC, PROTO, "protoc")
        cls.ctl = appctl.Server(cls.control)
        # Wait for its "listening" line (a connection to probe the port would
        # be taken for the controller's, and replaced by the next one).
        deadline = time.time() + 20
        while "listening on" not in (cls.dir / "server.log").read_text():
            if time.time() > deadline or cls.proc.poll() is not None:
                raise RuntimeError("the server didn't start: " + (cls.dir / "server.log").read_text())
            time.sleep(0.2)
        # The fake controller logs in; the server pushes the rest.
        cls.rabbit = FakeController(cls.port, cls.codec)
        token = cls.rabbit.until(r"authenticationToken")
        cls.rabbit.send(f'id: "c-auth" correlationId: "{re.search(r"^id: \"([^\"]*)\"", token, re.M).group(1)}" '
                        'authenticationResponse { login: "my-rabbit" authentication: "x" }')

    @classmethod
    def tearDownClass(cls):
        cls.receiver.close()
        cls.rabbit.close()
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def server_log(self):
        return (self.dir / "server.log").read_text()

    # ---- what is pushed after login ----

    def test_01_configuration_has_social_keys(self):
        config = self.rabbit.until(r"^config \{")
        self.assertIn('twitterConsumerKey: "ck"', config)
        self.assertIn('facebookToken: "fb"', config)
        self.assertNotIn("soundcloudToken", config)

    def test_02_contact_list_pushed(self):
        contacts = self.rabbit.until(r"^contactList \{")
        self.assertIn('firstname: "Ann"', contacts)
        self.assertIn('idDB: "o1"', contacts)
        self.assertIn('alias: "bob"', contacts)

    def test_03_pending_flag_set(self):
        self.assertIn("pendingmessage: true", self.rabbit.until(r"^pendingmessage:"))

    # ---- message queries an application makes (forwarded by the controller) ----

    def query(self, qid, body):
        self.rabbit.send(f'id: "{qid}" interactiveId: "ia" karotzMessageQuery {{ {body} }}')
        return self.rabbit.until(rf'correlationId: "{qid}"')

    def test_04_list_gives_headers(self):
        answer = self.query("q-list", "action: LIST")
        self.assertIn("code: OK", answer)
        self.assertEqual(re.findall(r'^\s+id: "(m\d)"', answer, re.M), ["m1", "m2"])
        self.assertNotIn("hello there", answer)  # headers only

    def test_05_get_gives_content(self):
        answer = self.query("q-get", 'action: GET id: "m1"')
        self.assertIn('content: "hello there"', answer)
        self.assertIn('header: "subject"', answer)
        self.assertNotIn('"m2"', answer)

    def test_06_read_marks_and_clears_pending(self):
        answer = self.query("q-read", 'action: READ id: "m1"')
        self.assertIn("status: READ", answer)
        self.assertIn("pendingmessage: false", self.rabbit.until(r"^pendingmessage:"))

    def test_07_new_message_is_stored(self):
        answer = self.query("q-new", 'action: NEW message { header { to { uuid: "u-ann" } } '
                                     'content { type: TEXT content: "from the rabbit" } }')
        self.assertIn("status: UNREAD", answer)
        self.assertIn("pendingmessage: true", self.rabbit.until(r"^pendingmessage:"))
        ok, listed = self.ctl("messages")
        self.assertTrue(ok)
        self.assertTrue(any("from the rabbit" in m for m in listed), listed)

    def test_08_delete_removes(self):
        self.query("q-del", 'action: DELETE id: "m2"')
        answer = self.query("q-list2", "action: LIST")
        self.assertNotIn('"m2"', answer)

    # ---- HTTP endpoints for applications ----

    def url(self, path):
        return f"http://127.0.0.1:{self.http}{path}"

    def test_10_text(self):
        self.assertEqual(urllib.request.urlopen(self.url("/text"), timeout=5).read(), b"hello from the app server")

    def test_11_log(self):
        ok, before = self.ctl("app-log")
        urllib.request.urlopen(self.url("/log?msg=step+one+ok"), timeout=5).read()
        time.sleep(0.3)
        self.assertIn("app: step one ok", self.server_log())
        # Kept for appctl.py app-log: the reports after a count.
        ok, after = self.ctl("app-log", str(before["count"]))
        self.assertTrue(ok)
        self.assertEqual(after["lines"], ["step one ok"])
        self.assertEqual(after["count"], before["count"] + 1)

    def test_12_raw_upload(self):
        req = urllib.request.Request(self.url("/upload/record"), data=b"fLaC\x00\x01", method="POST",
                                     headers={"Content-Type": "audio/flac"})
        self.assertEqual(urllib.request.urlopen(req, timeout=5).read(), b"OK")
        saved = list(self.uploads.glob("*-record.bin"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), b"fLaC\x00\x01")

    def test_13_multipart_upload(self):
        boundary = "XyZ"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"sendfile\"; filename=\"picture.jpg\"\r\n"
                f"Content-Type: image/jpeg\r\n\r\n").encode() + b"\xff\xd8JPEG" + \
               (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"filename\"\r\n\r\npicture.jpg"
                f"\r\n--{boundary}--\r\n").encode()
        req = urllib.request.Request(self.url("/upload/photo"), data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        urllib.request.urlopen(req, timeout=5).read()
        saved = list(self.uploads.glob("*-photo-picture.jpg"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), b"\xff\xd8JPEG")
        time.sleep(0.3)
        self.assertIn("filename='picture.jpg'", self.server_log())

    def test_14_unknown_post_is_404(self):
        req = urllib.request.Request(self.url("/nothing"), data=b"x", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 404)

    # ---- control commands ----

    def test_20_info_round_trip(self):
        result = {}

        def ask():
            result["answer"] = self.ctl("info")

        import threading
        t = threading.Thread(target=ask)
        t.start()
        query = self.rabbit.until(r"^karotzinfo_query")
        qid = re.search(r'^id: "([^"]*)"', query, re.M).group(1)
        self.rabbit.send(f'id: "c-info" correlationId: "{qid}" karotzinfo_response {{ infos {{ key: "version" value: "1" }} }}')
        t.join(15)
        ok, answer = result["answer"]
        self.assertTrue(ok)
        self.assertIn('value: "1"', answer)

    def test_21_system_command(self):
        ok, _ = self.ctl("system", "telnet_on")
        self.assertTrue(ok)
        self.assertIn("command: TELNET_ON", self.rabbit.until(r"^system \{"))
        ok, error = self.ctl("system", "nonsense")
        self.assertFalse(ok)

    def test_27_run_takes_the_first_instance(self):
        ok, answer = self.ctl("run", "pairapp")
        self.assertTrue(ok, answer)
        self.assertIn("pairapp (kitchen)", answer)
        self.rabbit.until(r'^scheduleConfig.*instance: "kitchen".*scheduleDate')
        ok, answer = self.ctl("run", "pairapp", "hall")
        self.assertTrue(ok, answer)
        self.assertIn("pairapp (hall)", answer)
        ok, error = self.ctl("run", "pairapp", "cellar")
        self.assertFalse(ok)
        self.assertIn("no instance 'cellar'", error)

    def test_22_pending_by_hand(self):
        self.ctl("pending", "off")
        # (Earlier tests' flags may still be waiting: look for this one.)
        self.rabbit.until(r"^pendingmessage: false")

    def test_25_take_as_a_chosen_id(self):
        t = self.answer_next(r'interactiveId: ""', "response { code: BAD_INTERACTIVEID }")
        ok, reply = self.ctl("take-as", "")
        t.join(15)
        self.assertTrue(ok)
        self.assertIn("action: START", self.answered)
        self.assertIn("BAD_INTERACTIVEID", reply)
        ok, status = self.ctl("status")
        self.assertNotEqual(status["interactive_id"], "")

    def test_26_send_raw_malformed(self):
        import threading
        # Cut off: the frame is one byte short of the message, and doesn't decode.
        sender = threading.Thread(target=self.ctl, args=("send-raw", "--cut", "1",
                                                         'interactiveId: "ia-raw" led { light { color: "FF0000" } }'))
        sender.start()
        cut = self.rabbit.raw_until(lambda p: b"ia-raw" in p)
        # Its answer wait (5 s) ends before the server is shut down.
        sender.join(15)
        with self.assertRaises(ValueError):
            self.rabbit.codec.decode(cut)
        self.assertTrue(cut.startswith(b"\x0a"))  # its id (field 1) is there
        # No id: the message starts with its interactive id (field 3), not field 1.
        ok, result = self.ctl("send-raw", "--no-id", 'interactiveId: "ia-noid" ping { }')
        self.assertTrue(ok)
        self.assertIn("without an id", result)
        noid = self.rabbit.raw_until(lambda p: b"ia-noid" in p)
        self.assertTrue(noid.startswith(b"\x1a"))
        ok, error = self.ctl("send-raw", "--bogus", "ping { }")
        self.assertFalse(ok)

    def test_23_events_since(self):
        ok, first = self.ctl("events-since")
        self.rabbit.send('id: "c-button" buttonCallback { type: SIMPLE }')
        time.sleep(0.5)
        ok, new = self.ctl("events-since", str(first["count"]))
        self.assertTrue(ok)
        self.assertEqual(len(new["events"]), 1)
        self.assertIn("buttonCallback", new["events"][0])

    def test_24_interactive_commands_need_interactive_mode(self):
        ok, error = self.ctl("photo", "http://x/upload")
        self.assertFalse(ok)
        self.assertIn("interactive", error)


    # ---- speech recognition (asr_server.py, --asr-port) ----

    def asr_session(self, grammar):
        """Play voice-daemon: the grammar, then audio until answered; the answer's fields."""
        import asr_server
        sock = socket.create_connection(("127.0.0.1", self.asr), timeout=10)
        with sock:
            def send(payload):
                sock.sendall(struct.pack(">I", len(payload)) + payload)
            send(asr_server.length_field(1, grammar.encode()))
            sock.settimeout(0.05)
            deadline = time.time() + 10
            answer = b""
            while time.time() < deadline:
                send(asr_server.length_field(2, bytes(1024)))  # 1024 bytes: 32 ms at 16 kHz
                try:
                    answer += sock.recv(4096)
                except socket.timeout:
                    pass
                if len(answer) >= 4 and len(answer) >= 4 + struct.unpack(">I", answer[:4])[0]:
                    break
                time.sleep(0.03)
        self.assertGreaterEqual(len(answer), 4, "no answer")
        message = answer[4:4 + struct.unpack(">I", answer[:4])[0]]
        response = next(v for n, _, v in asr_server.fields(message) if n == 3)
        return {n: v for n, _, v in asr_server.fields(response)}

    GRAMMAR = ("#ABNF 1.0 UTF-8;\r\nlanguage en-US;\r\nmode voice;\r\ntag-format <semantics/1.0>;\r\n"
               "root $App;\r\npublic $App = hello rabbit | good night;\r\n")

    def test_30_asr_answers_the_first_alternative(self):
        fields = self.asr_session(self.GRAMMAR)
        self.assertEqual(fields[1], 1)  # OK
        self.assertEqual(fields[3], b"hello rabbit")

    def test_31_asr_answer_set_by_command(self):
        ok, _ = self.ctl("asr-answer", "nomatch")
        self.assertTrue(ok)
        fields = self.asr_session(self.GRAMMAR)
        self.assertEqual(fields[1], 8)  # NOMATCH
        self.ctl("asr-answer", "first")

    def test_32_launch_grammar_served_and_answered_with_its_semantic(self):
        # The controller names its launch grammar by URL (http://<server>/asr/grammar/<id>.abnf, port
        # 80): this server's own grammar answers for it, with the application and instance the
        # controller starts. External applications aren't in it.
        grammar = urllib.request.urlopen(self.url("/asr/grammar/my-rabbit.abnf"), timeout=5).read().decode()
        self.assertIn("Hello {<app>hello</app><par>default</par>}", grammar)
        self.assertNotIn("extapp", grammar)
        url = "http://127.0.0.1:1/asr/grammar/my-rabbit.abnf"
        fields = self.asr_session(url)
        self.assertEqual((fields[1], fields[3]), (1, b"Device test"))
        self.assertEqual(fields[4], b"<app>devicetest</app><par>default</par>")
        self.ctl("asr-answer", "hello")
        try:
            fields = self.asr_session(url)
        finally:
            self.ctl("asr-answer", "first")
        self.assertEqual((fields[3], fields[4]), (b"hello", b"<app>hello</app><par>default</par>"))


    # ---- the REST API for external applications (rest_api.py) ----

    def rest(self, endpoint, **params):
        query = urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(self.url(f"/api/karotz/{endpoint}?{query}"), timeout=15) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def signed(self, **params):
        import base64, hashlib, hmac
        query = "&".join(f"{k}={params[k]}" for k in sorted(params))
        params["signature"] = base64.b64encode(hmac.new(b"s3cret", query.encode(), hashlib.sha1).digest()).decode()
        return params

    def answer_next(self, pattern, reply_body):
        """Answer, as the controller, the next message matching pattern."""
        import threading

        def answer():
            text = self.rabbit.until(pattern, timeout=15)
            mid = re.search(r'^id: "([^"]*)"', text, re.M).group(1)
            self.rabbit.send(f'id: "c-{mid[:8]}" correlationId: "{mid}" {reply_body}')
            self.answered = text
        t = threading.Thread(target=answer)
        t.start()
        return t

    def test_40_rest_start_refuses_a_bad_signature(self):
        code, xml = self.rest("start", apikey="extapp", installid="inst-ext", once="1", timestamp="1",
                              signature="bad")
        self.assertEqual(code, 403)
        self.assertIn("bad signature", xml)

    def test_41_rest_start_command_callback_stop(self):
        t = self.answer_next(r"^interactiveMode \{", "response { code: OK }")
        code, xml = self.rest("start", **self.signed(apikey="extapp", installid="inst-ext", once="7", timestamp="1"))
        t.join(15)
        self.assertEqual(code, 200, xml)
        self.assertIn('configId: "inst-ext"', self.answered)
        self.assertIn('access: "led"', self.answered)
        iid = re.search(r"<interactiveId>([^<]+)</interactiveId>", xml).group(1)
        # A command, answered as XML.
        t = self.answer_next(r"^led \{", "response { code: OK }")
        code, xml = self.rest("led", interactiveid=iid, action="pulse", color="00FF00", period="500", pulse="3000")
        t.join(15)
        self.assertEqual(code, 200, xml)
        self.assertIn("<response><code>OK</code></response>", xml)
        self.assertIn('color: "00FF00"', self.answered)
        self.assertIn("pulse: 3000", self.answered)
        # Its event, later, goes to the application's callback.
        mid = re.search(r'^id: "([^"]*)"', self.answered, re.M).group(1)
        self.rabbit.send(f'id: "c-ev" correlationId: "{mid}" interactiveId: "{iid}" event {{ code: TERMINATED }}')
        deadline = time.time() + 10
        while not any("TERMINATED" in c for c in self.callbacks) and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(any("<event><code>TERMINATED</code></event>" in c for c in self.callbacks), self.callbacks)
        # A bad command is refused without reaching the rabbit.
        code, xml = self.rest("led", interactiveid=iid, color="nothex")
        self.assertEqual(code, 400)
        # The stop.
        t = self.answer_next(r"action: STOP", "response { code: OK }")
        code, xml = self.rest("interactivemode", interactiveid=iid, action="stop")
        t.join(15)
        self.assertEqual(code, 200, xml)


class CallbackReceiver:
    """An external application's server: collects what the platform POSTs to its callback."""

    def __init__(self, store):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                store.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
