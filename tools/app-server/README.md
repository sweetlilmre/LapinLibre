<!-- SPDX-License-Identifier: MIT -->
# app-server: a local platform for the rabbit

The app server is the platform server for the rabbit. You run it on your own computer. The rabbit's controller connects to it and gets its applications, schedules and commands from it. The folder also has the speech server (`tts_server.py`), the speech-recognition server (`asr_server.py`), the control tool (`appctl.py`) and the REST API (`rest_api.py`).

The full documentation is in [docs/app-server.md](../../docs/app-server.md): how to install and start it, every option and command, the settings file, the applications folder, the ports and the security notes.

Quick start:

```
cd tools/app-server
uv sync
uv run --with grpcio-tools python -m grpc_tools.protoc -I ../../userland/proto --python_out=. platform-message.proto
uv run python app_server.py --apps example-apps --settings settings.example.json --tts-port 8081 --asr-port 8443
uv run python appctl.py status
uv run python appctl.py run hello
```

The server has no authentication. Run it on your own machine, on a trusted network.
