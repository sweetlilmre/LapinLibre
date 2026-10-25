# /// script
# requires-python = ">=3.11"
# dependencies = ["kokoro-onnx", "lameenc"]
# ///
# SPDX-License-Identifier: MIT
"""The texts of the key's spoken install prompts (image/usbkey/sound/*.mp3, PROMPTS), which the image
build copies to the key. The kept files are spoken by image/tools/speech_sounds.py, in
the rabbit's English voice (Qwen3-TTS cloning bf_emma); this script speaks them with Kokoro directly.

    uv run image/tools/install_prompts.py [--voice bf_emma] [--models DIR] [NAME.mp3 ...]
    uv run image/tools/install_prompts.py --reference OUT.wav TEXT

Kokoro-82M (Apache-2.0 weights) through kokoro-onnx (MIT) speaks each text; the samples are evened to
the same peak and encoded as 64 kbit/s mono MP3 with lameenc, which the install's madplay plays. The
model files come from kokoro-onnx's own release and are checked against MODEL_SHA256 before use.
image/usbkey/sound/README.md records how the files were made.

--reference writes TEXT in the voice as a 16-bit WAV instead: the English reference clip that
image/tools/speech_sounds.py clones for the rabbit's English spoken sounds.
"""

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "image" / "usbkey" / "sound"
PROMPTS = {
    "install-start.mp3": "Welcome to the installation of the Lapin Libre OS, please wait while the OS is installed.",
    "install-done.mp3": "The installation is complete. I am going to restart to connect to the internet, "
                      "please wait until the light is green.",
    "checks-start.mp3": "Checking the Lapin Libre install, please wait.",
    "checks-passed.mp3": "Install checks passed.",
    "checks-failed.mp3": "Warning. The install checks failed. Please recreate the USB stick and try again.",
}
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_SHA256 = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}


def model_file(models, name):
    path = models / name
    if not path.is_file():
        models.mkdir(parents=True, exist_ok=True)
        print(f"downloading {name}", file=sys.stderr)
        urllib.request.urlretrieve(RELEASE + name, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    want = MODEL_SHA256[name]
    if want is None:
        print(f"{name}: sha256 {digest} (not pinned yet)", file=sys.stderr)
    elif digest != want:
        raise SystemExit(f"{name}: sha256 {digest}, expected {want}")
    return path


def main(argv):
    p = argparse.ArgumentParser(description="The key's spoken install prompts.")
    p.add_argument("--voice", default="bf_emma")
    p.add_argument("--models", type=Path, default=Path.home() / ".cache" / "lapinlibre" / "kokoro")
    p.add_argument("--reference", type=Path, help="write the first name given, as text, to this WAV instead")
    p.add_argument("names", nargs="*", help="only these prompts (default: all)")
    args = p.parse_args(argv)
    import lameenc
    import numpy as np
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(model_file(args.models, "kokoro-v1.0.onnx")), str(model_file(args.models, "voices-v1.0.bin")))
    lang = "en-gb" if args.voice.startswith("b") else "en-us"
    if args.reference:
        import wave
        if len(args.names) != 1:
            raise SystemExit("--reference takes the text as its one argument")
        samples, rate = kokoro.create(args.names[0], voice=args.voice, speed=1.0, lang=lang)
        samples = np.asarray(samples, dtype=np.float32)
        samples = samples / max(1e-6, float(np.abs(samples).max())) * 0.89
        with wave.open(str(args.reference), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((samples * 32767).astype("<i2").tobytes())
        print(f"{args.reference}: {len(samples) / rate:.1f} s at {rate} Hz ({args.voice})")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in PROMPTS.items():
        if args.names and name not in args.names:
            continue
        samples, rate = kokoro.create(text, voice=args.voice, speed=1.0, lang=lang)
        samples = np.asarray(samples, dtype=np.float32)
        samples = samples / max(1e-6, float(np.abs(samples).max())) * 0.89  # about -1 dB peak
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(64)
        encoder.set_in_sample_rate(rate)
        encoder.set_channels(1)
        encoder.set_quality(2)
        data = encoder.encode((samples * 32767).astype("<i2").tobytes()) + encoder.flush()
        (OUT / name).write_bytes(bytes(data))
        print(f"{name}: {len(data)} bytes, {len(samples) / rate:.1f} s at {rate} Hz ({args.voice})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
