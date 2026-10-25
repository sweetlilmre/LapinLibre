# SPDX-License-Identifier: MIT
"""The rabbit's spoken sounds (image/core/usr/karotz/sounds/*.mp3) and the key's spoken install prompts
(image/usbkey/sound/*.mp3), made once and kept in the repo: the controller plays the first when a tag
isn't known, when speech isn't understood, and when the speech recognition server doesn't answer; the
key's install plays the prompts (their texts are install_prompts.py's PROMPTS), in the English voice.

    tools/qwen-tts/.venv/Scripts/python -I -B image/tools/speech_sounds.py [--models DIR] [--voices DIR] [NAME.mp3 ...]

Qwen3-TTS (Apache-2.0 code and weights, the qwen-tts package) speaks each text, on the CPU: for each
language, a reference clip of a female voice saying the language's reference sentence is kept in
--voices and reused, so a later run keeps the voice; the Base model then speaks every text of the
language in that voice. French, German and Spanish's clips are made by the VoiceDesign model from
VOICES' description. English's is Kokoro's bf_emma (British English), because VoiceDesign's
"British" came out American: make it first, with
  uv run image/tools/install_prompts.py --reference ~/.cache/lapinlibre/qwen/voices/en.wav "<the en sentence>"
The samples are evened to the same peak and encoded as 64 kbit/s mono MP3 with lameenc, which the rabbit's madplay plays. The
models are the pinned revisions of MODELS, checked against their sha256 before use, and run offline;
they are fetched once with the venv's hf command (the script prints it when one is missing). Sampling
is on, as the models expect, with the seed SEED: a new run gives a voice and timing close to the
kept files, not the same bytes, so the MP3s in the repo are the record.

The venv (git-ignored): uv venv tools/qwen-tts/.venv, then uv pip install qwen-tts==0.1.1
lameenc==1.9.0 "huggingface_hub[cli]" into it (torch, transformers, librosa and soundfile come with
qwen-tts).
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "image" / "core" / "usr" / "karotz" / "sounds"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_prompts import OUT as INSTALL_OUT, PROMPTS  # noqa: E402
SEED = 20261020
# The model folders, their Hugging Face revision, and the sha256 of their weights.
MODELS = {
    "VoiceDesign": ("Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", "5ecdb67327fd37bb2e042aab12ff7391903235d3", {
        "model.safetensors": "391e8db219f292c515297cdceeb43e4eae67cdde35fa57e79a6a8a532fca0522",
        "speech_tokenizer/model.safetensors": "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258"}),
    "Base": ("Qwen/Qwen3-TTS-12Hz-1.7B-Base", "fd4b254389122332181a7c3db7f27e918eec64e3", {
        "model.safetensors": "38fc7fc51c5e776e840414b6fd443962e9411b9654888fd7913e4da643cb857c",
        "speech_tokenizer/model.safetensors": "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258"}),
}
VOICE = "A woman of about thirty with a warm, friendly voice, medium pitch, speaking clearly at a calm pace"
# Each language's voice: Qwen's language, the accent added to VOICE, and the sentence the voice is
# made from.
VOICES = {
    # English's reference clip is Kokoro's bf_emma (install_prompts.py --reference), not designed.
    "en": ("English", "with a natural British English accent",
           "Hello! I'm your rabbit. Hold a tag up to my nose, or press my head and talk to me."),
    "fr": ("French", "a native French speaker from France",
           "Bonjour ! Je suis ton lapin. Approche un objet de mon nez, ou appuie sur ma tête pour me parler."),
    "de": ("German", "a native German speaker from Germany",
           "Hallo! Ich bin Ihr Hase. Halten Sie ein Objekt an meine Nase oder drücken Sie auf meinen Kopf."),
    "es": ("Spanish", "a native Spanish speaker from Spain",
           "¡Hola! Soy tu conejo. Acerca un objeto a mi nariz o pulsa mi cabeza para hablarme."),
}
# The files the controller plays, by language: an unknown tag, speech not understood, and the speech
# recognition server not answering (en-GB and fr-FR only: the controller plays en-GB's for the
# other languages).
SOUNDS = {
    "en_tag_unknown.mp3": ("en", "Sorry, I don't recognise this object, because it hasn't been set up yet."),
    "en_speech_not_understood.mp3": ("en", "Sorry, I didn't understand you. Please try again."),
    "speech_server_timeout_en-GB.mp3": ("en", "I'm sorry, the voice recognition service isn't available at the moment. "
                                           "Please try again later."),
    "fr_tag_unknown.mp3": ("fr", "Désolée, je ne reconnais pas cet objet, car il n'est pas encore configuré."),
    "fr_speech_not_understood.mp3": ("fr", "Je n'ai pas compris ce que tu as dit. Tu peux répéter ?"),
    "speech_server_timeout_fr-FR.mp3": ("fr", "Je suis désolée, le serveur de reconnaissance vocale est actuellement "
                                           "indisponible. Essaie à nouveau un peu plus tard."),
    "de_tag_unknown.mp3": ("de", "Ich kenne dieses Objekt leider nicht, weil es noch nicht konfiguriert ist."),
    "de_speech_not_understood.mp3": ("de", "Ich habe Sie nicht verstanden. Würden Sie das bitte wiederholen?"),
    "es_tag_unknown.mp3": ("es", "Lo siento, pero no reconozco este objeto porque no está configurado."),
    "es_speech_not_understood.mp3": ("es", "Lo siento, pero no lo he entendido. ¿Puede repetirlo?"),
}
# The key's install prompts, in English: their texts and file names are install_prompts.py's.
INSTALL = {name: ("en", text) for name, text in PROMPTS.items()}
ALL = {**SOUNDS, **INSTALL}


def model_dir(models, name):
    repo, revision, sums = MODELS[name]
    path = models / name
    for rel, want in sums.items():
        f = path / rel
        if not f.is_file():
            raise SystemExit(f"no {f}: fetch it with\n  hf download {repo} --revision {revision} --local-dir {path}")
        h = hashlib.sha256()
        with open(f, "rb") as fp:
            for block in iter(lambda: fp.read(1 << 24), b""):
                h.update(block)
        if h.hexdigest() != want:
            raise SystemExit(f"{f}: sha256 {h.hexdigest()}, expected {want}")
    return str(path)


def mp3(samples, rate):
    import lameenc
    import numpy as np

    samples = np.asarray(samples, dtype=np.float32)
    samples = samples / max(1e-6, float(np.abs(samples).max())) * 0.89  # about -1 dB peak
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(64)
    encoder.set_in_sample_rate(rate)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return bytes(encoder.encode((samples * 32767).astype("<i2").tobytes()) + encoder.flush())


def main(argv):
    p = argparse.ArgumentParser(description="The rabbit's spoken sounds.")
    p.add_argument("--models", type=Path, default=Path.home() / ".cache" / "lapinlibre" / "qwen")
    p.add_argument("--voices", type=Path, default=None, help="the voices' reference clips (default: MODELS/voices)")
    p.add_argument("names", nargs="*", help="only these sounds or prompts (default: all)")
    args = p.parse_args(argv)
    voices = args.voices or args.models / "voices"
    wanted = {n: ALL[n] for n in (args.names or ALL)}
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import soundfile
    import torch
    from qwen_tts import Qwen3TTSModel

    kw = {"device_map": "cpu", "dtype": torch.float32}
    langs = sorted({lang for lang, _ in wanted.values()})
    voices.mkdir(parents=True, exist_ok=True)
    missing = [lang for lang in langs if not (voices / f"{lang}.wav").is_file()]
    if missing:
        design = Qwen3TTSModel.from_pretrained(model_dir(args.models, "VoiceDesign"), **kw)
        for lang in missing:
            language, accent, sentence = VOICES[lang]
            torch.manual_seed(SEED)
            wavs, rate = design.generate_voice_design(text=sentence, language=language, instruct=f"{VOICE}, {accent}.")
            soundfile.write(voices / f"{lang}.wav", wavs[0], rate)
            print(f"voice {lang}: {len(wavs[0]) / rate:.1f} s at {rate} Hz")
        del design
    base = Qwen3TTSModel.from_pretrained(model_dir(args.models, "Base"), **kw)
    for lang in langs:
        language, _, sentence = VOICES[lang]
        ref, ref_rate = soundfile.read(voices / f"{lang}.wav", dtype="float32")
        prompt = base.create_voice_clone_prompt(ref_audio=(ref, ref_rate), ref_text=sentence)
        for name, (l, text) in wanted.items():
            if l != lang:
                continue
            torch.manual_seed(SEED)
            wavs, rate = base.generate_voice_clone(text=text, language=language, voice_clone_prompt=prompt)
            data = mp3(wavs[0], rate)
            out = INSTALL_OUT if name in INSTALL else OUT
            out.mkdir(parents=True, exist_ok=True)
            (out / name).write_bytes(data)
            print(f"{name}: {len(data)} bytes, {len(wavs[0]) / rate:.1f} s at {rate} Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
