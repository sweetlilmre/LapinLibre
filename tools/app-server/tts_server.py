# SPDX-License-Identifier: MIT
"""tts_server: a local speech server for the rabbit's voice-daemon (runs on the PC, in WSL).

voice-daemon speaks a text by POSTing it to the URL that
/usr/etc/conf/voice.conf gives for the language ([tts] <lang>=<url>), wrapped
in SSML-style breaks ("<s><break time="200ms" />text<break .../></s>"), and
piping the answer, an MP3, into madplay. This server answers such a POST on
/<lang> with speech it synthesises locally:

- Kokoro-82M (Apache-2.0 weights) through kokoro-onnx, when the package is
  installed (uv sync in this folder) and its model files are in
  ~/.cache/lapinlibre/kokoro (uv run image/tools/install_prompts.py fetches
  and checks them): English (bf_emma, the voice of the key's prompts),
  French, Spanish, Italian and Portuguese;
- for the other languages (German, Dutch), or without Kokoro, espeak-ng if
  it is installed (every language);
- otherwise Windows' own speech engine (System.Speech, reached through
  powershell.exe from WSL), with a voice of the language if one is
  installed and the default voice otherwise;

then encodes it to MP3, in-process with the lameenc package when it is
installed (uv sync in this folder, see pyproject.toml), otherwise with the
ffmpeg command. Runs natively on Windows as well as in WSL.

    python3 tts_server.py [--listen 0.0.0.0] [--port 8081] [--engine auto|kokoro|espeak|windows] [--models DIR]
    python3 tts_server.py --selftest      # synthesise one sentence, check the MP3

app_server.py --tts-port 8081 runs it alongside the platform server. Point
each language of voice.conf's [tts] section at http://<this machine>:8081/<lang>
(README.md, "Pointing the rabbit at it"). Local only: anything that
can reach the port can make it speak.
"""

import argparse
import base64
import hashlib
import html
import io
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Windows' System.Speech, driven through powershell.exe. The text arrives
# base64-encoded, raw PCM (22050 Hz, 16-bit, mono) leaves on stdout, the
# voice name on stderr.
WINDOWS_SCRIPT = """
$ProgressPreference = 'SilentlyContinue'
$t = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{text}'))
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$v = $s.GetInstalledVoices() | Where-Object {{ $_.Enabled -and $_.VoiceInfo.Culture.Name -like '{lang}*' }} | Select-Object -First 1
if ($v) {{ $s.SelectVoice($v.VoiceInfo.Name) }}
[Console]::Error.WriteLine('VOICE:' + $s.Voice.Name)
$m = New-Object IO.MemoryStream
$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToAudioStream($m, $f)
$s.Speak($t)
$o = [Console]::OpenStandardOutput()
$m.WriteTo($o)
$o.Flush()
"""


def log(text):
    print(time.strftime("%H:%M:%S"), text, flush=True)


def plain_text(body):
    """The text inside voice-daemon's SSML-style wrapper."""
    return html.unescape(re.sub(r"<[^>]*>", " ", body)).strip()


def synth_espeak(text, lang):
    r = subprocess.run(["espeak-ng", "-v", lang, "--stdout", text], capture_output=True, check=True)
    return r.stdout, f"espeak-ng {lang}"


WINDOWS_RATE = 22050


def wav_of(pcm, rate, channels=1):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def synth_windows(text, lang):
    script = WINDOWS_SCRIPT.format(text=base64.b64encode(text.encode()).decode(), lang=re.sub(r"[^a-z-]", "", lang))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                       capture_output=True, check=True)
    voice = re.search(r"VOICE:(.*)", r.stderr.decode(errors="replace"))
    return wav_of(r.stdout, WINDOWS_RATE), f"windows, {voice.group(1).strip() if voice else 'default voice'}"


ENGINES = {"espeak": (synth_espeak, "espeak-ng"), "windows": (synth_windows, "powershell.exe")}


# Kokoro's voice and phoneme language for each voice.conf language it speaks.
KOKORO_VOICES = {"en": ("bf_emma", "en-gb"), "fr": ("ff_siwis", "fr-fr"), "es": ("ef_dora", "es"),
                 "it": ("if_sara", "it"), "pt": ("pf_dora", "pt-br")}
KOKORO_MODELS = Path.home() / ".cache" / "lapinlibre" / "kokoro"
# The model files image/tools/install_prompts.py fetches and checks.
KOKORO_SHA256 = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}


class KokoroEngine:
    """Kokoro-82M, loaded once; RuntimeError when kokoro-onnx or its checked model files are missing."""

    def __init__(self, models):
        try:
            import numpy
            from kokoro_onnx import Kokoro
        except ImportError:
            raise RuntimeError("kokoro-onnx is not installed (uv sync in this folder)") from None
        for name, want in KOKORO_SHA256.items():
            path = models / name
            if not path.is_file():
                raise RuntimeError(f"no {path} (uv run image/tools/install_prompts.py fetches it)")
            if hashlib.sha256(path.read_bytes()).hexdigest() != want:
                raise RuntimeError(f"{path}: not the expected file (sha256)")
        self.np = numpy
        self.kokoro = Kokoro(str(models / "kokoro-v1.0.onnx"), str(models / "voices-v1.0.bin"))
        self.lock = threading.Lock()

    @staticmethod
    def speaks(lang):
        return lang.split("-")[0].lower() in KOKORO_VOICES

    def __call__(self, text, lang):
        voice, code = KOKORO_VOICES[lang.split("-")[0].lower()]
        with self.lock:
            samples, rate = self.kokoro.create(text, voice=voice, speed=1.0, lang=code)
        samples = self.np.asarray(samples, dtype=self.np.float32)
        samples = samples / max(1e-6, float(self.np.abs(samples).max())) * 0.89  # about -1 dB peak
        return wav_of((samples * 32767).astype("<i2").tobytes(), rate), f"kokoro {voice}"


def pick_engine(name, models=KOKORO_MODELS):
    """The synthesis function and the engines' names. auto: Kokoro for the languages it speaks,
    the first of espeak-ng and Windows' speech that is installed for the others (and for every
    language without Kokoro); kokoro: Kokoro alone."""
    kokoro, other, names = None, None, []
    if name in ("auto", "kokoro"):
        try:
            kokoro = KokoroEngine(models)
            names.append("kokoro")
        except RuntimeError as e:
            if name == "kokoro":
                raise
            log(f"no Kokoro: {e}")
    if name != "kokoro":
        for key in (["espeak", "windows"] if name == "auto" else [name]):
            fn, tool = ENGINES[key]
            if shutil.which(tool):
                other = fn
                names.append(key)
                break
    if not names:
        raise RuntimeError(f"no speech engine ({'Kokoro, espeak-ng or powershell.exe' if name == 'auto' else ENGINES[name][1]} not found)")

    def synth(text, lang):
        if kokoro is not None and kokoro.speaks(lang):
            return kokoro(text, lang)
        if other is None:
            raise RuntimeError(f"Kokoro has no voice for {lang!r}")
        return other(text, lang)
    return synth, " and ".join(names)


try:
    import lameenc
except ImportError:
    lameenc = None


def to_mp3(wav):
    """16-bit WAV to MP3 (64 kbit/s), with lameenc if installed, else ffmpeg."""
    if lameenc is not None:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() == 2:
                encoder = lameenc.Encoder()
                encoder.set_bit_rate(64)
                encoder.set_in_sample_rate(w.getframerate())
                encoder.set_channels(w.getnchannels())
                encoder.set_quality(2)
                # espeak-ng's header gives no length: read what there is.
                pcm = w.readframes(1 << 30)
                return bytes(encoder.encode(pcm) + encoder.flush())
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "-", "-ac", "1", "-ar", "22050",
                        "-b:a", "64k", "-f", "mp3", "-"], input=wav, capture_output=True, check=True)
    return r.stdout


class Speech:
    def __init__(self, engine):
        self.engine, self.cache, self.lock = engine, {}, threading.Lock()

    def mp3(self, text, lang):
        key = (text, lang)
        with self.lock:
            if key in self.cache:
                return self.cache[key], "cached"
        wav, voice = self.engine(text, lang)
        mp3 = to_mp3(wav)
        with self.lock:
            self.cache[key] = mp3
        return mp3, voice


class Handler(BaseHTTPRequestHandler):
    speech: Speech

    def do_POST(self):
        lang = self.path.strip("/").split("?")[0] or "en"
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
        text = plain_text(body)
        started = time.time()
        try:
            mp3, voice = self.speech.mp3(text, lang)
        except (subprocess.CalledProcessError, OSError, RuntimeError) as e:
            log(f"{lang}: {text!r}: synthesis failed ({e})")
            self.send_error(500)
            return
        log(f"{lang}: {text!r} -> {len(mp3)} bytes ({voice}, {time.time() - started:.1f} s)")
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(mp3)))
        self.end_headers()
        self.wfile.write(mp3)

    def log_message(self, format, *args):
        pass


def is_mp3(data):
    """An MPEG audio frame sync at the start (ffmpeg writes no ID3 tag with -f mp3 to a pipe, but allow one)."""
    if data[:3] == b"ID3":
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        data = data[10 + size:]
    return len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def speech_server(listen, port, engine="auto", models=KOKORO_MODELS):
    """The HTTP server, ready to serve_forever(); RuntimeError when there is
    no MP3 encoder (lameenc or ffmpeg) or no speech engine."""
    if lameenc is None and not shutil.which("ffmpeg"):
        raise RuntimeError("no MP3 encoder (the lameenc package or ffmpeg)")
    synth, engines = pick_engine(engine, models)
    log(f"speech engines: {engines}")
    Handler.speech = Speech(synth)
    threading.Thread(target=warm_up, args=(synth,), daemon=True).start()
    return ThreadingHTTPServer((listen, port), Handler)


def warm_up(synth):
    """One throwaway sentence, so the engine has loaded before the first request: voice-daemon gives
    up on an answer that takes a few seconds, and the first synthesis does."""
    started = time.time()
    try:
        synth("Ready.", "en")
    except (subprocess.CalledProcessError, OSError, RuntimeError) as e:
        log(f"warm-up failed ({e})")
        return
    log(f"speech engine warmed up ({time.time() - started:.1f} s)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--engine", choices=["auto", "kokoro", "espeak", "windows"], default="auto")
    p.add_argument("--models", type=Path, default=KOKORO_MODELS, help="Kokoro's model files")
    p.add_argument("--selftest", action="store_true", help="synthesise one sentence and check the MP3")
    a = p.parse_args()
    try:
        # The self-test serves nothing: an unused local port, so it runs beside a running server.
        httpd = speech_server("127.0.0.1" if a.selftest else a.listen, 0 if a.selftest else a.port, a.engine, a.models)
    except (RuntimeError, OSError) as e:
        sys.exit(f"tts_server: {e}")
    if a.selftest:
        mp3, voice = Handler.speech.mp3(plain_text('<s><break time="200ms" />Hello from the rabbit.<break time="150ms" /></s>'), "en")
        ok = is_mp3(mp3) and len(mp3) > 2000
        print(f"{'OK' if ok else 'FAIL'}: {len(mp3)} bytes of MP3 ({voice})")
        sys.exit(0 if ok else 1)
    log(f"speech server on {a.listen}:{a.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
