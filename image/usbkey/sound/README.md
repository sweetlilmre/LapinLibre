<!-- SPDX-License-Identifier: MIT -->
# The key's spoken install prompts

`checks-start.mp3`, `checks-passed.mp3`, `checks-failed.mp3`, `install-start.mp3` and `install-done.mp3` are spoken by [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) (code and weights under Apache-2.0) in the rabbit's English voice: its Base model clones a reference clip of Kokoro-82M's `bf_emma` voice (British English), the same voice as the rabbit's own English sounds. They are encoded with lameenc as 64 kbit/s mono MP3, which the install's madplay plays, and were made on 2026-10-22 with `image/tools/speech_sounds.py` (the texts are `PROMPTS` in `image/tools/install_prompts.py`, LapinLibre's own). `speech_sounds.py` records the models' revisions and sha256s.

The other install sounds (`Usb`, `Loop`, `Error`) are tones that the image build generates (`INSTALL_SOUNDS` in `image/tools/image.py`).
