<!-- SPDX-License-Identifier: MIT -->
# LapinLibre technical documentation

This page lists the technical documents and tells you where to start.

## How to find a task

| You want to… | Read |
|---|---|
| Build LapinLibre from source, or run the tests | [build.md](build.md) |
| Put LapinLibre on a rabbit, update it, or change its settings | [install.md](install.md) |
| Control the rabbit from a browser, a script or a program over HTTP | [web-api.md](web-api.md) |
| Run applications on the rabbit from your own computer, with speech and speech recognition | [app-server.md](app-server.md) |
| Know how the system on the rabbit works: the boot, the daemons, D-Bus, the controller and the JavaScript API for applications | [system.md](system.md) |
| Know which sound plays when | [sound-map.md](sound-map.md) |

Each document starts with a **How to** section: short procedures with the commands to type. The **Technical details** section below it tells you how each part works.

## The parts of LapinLibre

LapinLibre has three parts:

- **The build** runs on a Linux computer. It makes the toolchain, the kernel, the programs and the USB key. See [build.md](build.md).
- **The rabbit's system** runs on the rabbit. The install writes it from the USB key. It has the daemons, the controller, the web page and the API. See [install.md](install.md), [system.md](system.md) and [web-api.md](web-api.md).
- **The app server** runs on your own computer, if you want it. It gives the rabbit its applications, the speech and the speech recognition. See [app-server.md](app-server.md).

## The repository

| Path | Contents |
|---|---|
| `build.sh` | The build's entry point. It runs the build stages. |
| `config.toml` | The versions, the downloads with their checksums, and the size limits of the flash. |
| `scripts/` | The shared shell settings (`common.sh`), the check of the build computer and the check of the repository. |
| `system/` | The Buildroot configuration and packages, and the builds of V8, the kernel, the programs and the install tools. Also the check of the image. |
| `kernel/` | The rabbit's own kernel sources (the board and its drivers). The build applies them to Linux 2.6.32. |
| `userland/` | The rabbit's programs in C and C++ (`src/`), their D-Bus interfaces (`dbus/`) and the platform protocol (`proto/`). |
| `image/` | The files of the rabbit's system (`core/`), the USB key's install (`usbkey/`) and the tools that make the image (`tools/`). |
| `tests/` | The tests: behaviour scenarios and aggressive tests under qemu-user, the image tests, the app server tests and the tests for a real rabbit. |
| `tools/app-server/` | The app server for your own computer. |
| `docs/` | These documents. |
| `LICENSES/` | The licence texts. |
