<!-- SPDX-License-Identifier: MIT -->
# Liberté, Égalité, Carotte

This is a love letter to my poor karotz that I found abandoned in a box, yellowed by time and the sun.
I have attempted to fully recreate what I can of the OS, or perhaps what it could have been.
This is a massive work in progress, there is a newer kernel, many functions have been fixed or upgraded and undoubtedly there are many bugs.

LapinLibre is free firmware for the Karotz (Nabaztag v3) rabbit. It builds the whole system from source and puts it on a USB key that installs it on the rabbit. The rabbit then works without the original cloud service. It has its own web page and an OpenKarotz-compatible API at `http://lapinlibre.local/`.

The technical documents are in [docs/index.md](docs/index.md): the build, the install, the web API, the app server and how the system works.

## What you need

- A Karotz rabbit with its power supply.
- A USB stick formatted as FAT32.
- A Wi-Fi network with WPA or WPA2.
- Only to build LapinLibre yourself: a computer with Ubuntu 24.04 or another Debian-like Linux, natively or in WSL 2.

## Get the USB key

You don't have to build LapinLibre: each release has the USB key ready to use.

1. Open the [releases page](https://github.com/sweetlilmre/LapinLibre/releases) and download `LapinLibre-<version>.zip` from the latest release. The `.sha256` file beside it lets you check the download: `sha256sum -c LapinLibre-<version>.zip.sha256`.
2. Extract the zip. Its files are the contents of the USB key.

To build the key yourself instead, see [Build LapinLibre yourself](#build-lapinlibre-yourself).

## Install LapinLibre on the rabbit

1. Copy the files of the key (from the zip, or from `out/usbkey` if you built it) to the root of the empty USB stick.
2. Open `lapinlibre.txt` on the stick in a text editor and fill in the settings (below). Type each value after its `=` sign.
3. Save the file and put the stick in the rabbit.
4. Switch on the rabbit. The nose pulses yellow and the install plays its sounds. The rabbit restarts when the install is complete.
5. Open `http://lapinlibre.local/` in a web browser. If you set `HOSTNAME`, use that name. If you set a password, log in as `admin`.

**Decide now how you will get into the rabbit.** The web page doesn't give you a shell. The only ways to log in to the rabbit are SSH with the key in `SSH_PUBKEY` (there is no password login) and telnet with `TELNET=yes`. With neither, there is no way into the rabbit from your network. To add a key later, put the stick back with only `SSH_PUBKEY` filled in and switch the rabbit on: the install applies the settings without installing again.

### The settings in lapinlibre.txt

An empty value keeps what the rabbit already has from an earlier install. A first install needs the Wi-Fi network; everything else is optional.

| Setting | What to type | What it does |
|---|---|---|
| `WIFI_SSID`, `WIFI_PASSWORD` | Your Wi-Fi network's name and its WPA or WPA2 password (8 to 63 characters). Set both or neither. | The network the rabbit joins. Needed for a first install. |
| `IP`, `NETMASK`, `GATEWAY`, `DNS` | Either nothing, or a fixed address for the rabbit (for example `192.168.1.10`), with your router's address in `GATEWAY` and a DNS server in `DNS` (`NETMASK` is already filled in with `255.255.255.0`). | How you find the rabbit on your network; see below. |
| `SSH_PUBKEY` | Your SSH public key, all of it on one line (`ssh-ed25519 AAAA... you@pc`, or an `ssh-rsa` or `ecdsa` key). | Lets you log in with `ssh karotz@lapinlibre.local`, as root. Only this key is accepted: there is no password login. |
| `WEB_PASSWORD` | 8 to 64 characters, without spaces, `"`, `\` or `:`. | The web page and its whole API, the OpenKarotz-compatible `cgi-bin` calls included, ask for the user `admin` and this password. Programs written for OpenKarotz (Home Assistant integrations, for example) send no login, so with a password they stop working unless they can be given one. Leave it empty for no password: this keeps the default OpenKarotz compatibility, but the page and the API are open to everyone on your network. Unlike the other settings, an empty value does not keep what the rabbit has: if the rabbit already has a password, leaving this empty removes it. |
| `TELNET` | `yes` or `no`. | `yes` gives anyone on your network a root shell with no password. Use it for testing only. Empty keeps the rabbit's setting (`no` unless an earlier install said `yes`). |
| `SERVER` | The address of the computer that runs the app server, for example `192.168.1.10`. | The rabbit gets its applications, speech and speech recognition from that computer. See [docs/app-server.md](docs/app-server.md). |
| `TTS_SERVER`, `ASR_SERVER` | A URL like `http://192.168.1.10:8081`, and `host:port` like `192.168.1.10:8443`. | Only if speech or speech recognition run on another computer than `SERVER`. |
| `LANGUAGE` | For example `en-US`, `en-GB`, `fr-FR`, `de-DE` or `es-ES`. | The rabbit's language. |
| `HOSTNAME` | Lower-case letters, digits and dashes. | The rabbit's name on your network: it answers to `<name>.local`. The default is `lapinlibre`. Give each rabbit its own. |

**How to find the rabbit on your network.** Choose one of the two:

- **A fixed address:** set `IP`, `GATEWAY` and `DNS` (and `NETMASK` if yours isn't `255.255.255.0`). The rabbit always has that address: use `http://<IP>/` and `ssh karotz@<IP>`. Pick an address outside your router's DHCP range, so that no other device gets it.
- **An address from the router:** leave `IP` empty. The router gives the rabbit an address, which can change, and you reach the rabbit by its name: `http://<HOSTNAME>.local/` and `ssh karotz@<HOSTNAME>.local` (`lapinlibre.local` unless you set `HOSTNAME`). This needs mDNS on your computer: Windows 10 and later, macOS, and Linux with Avahi have it. A program that can't look up `.local` names needs a fixed address, or a reservation for the rabbit in your router.

To update, make a key with the new release (or your new build) and do the same steps. You can leave `lapinlibre.txt` empty: the rabbit keeps its settings, its applications and its messages. For the install's details, errors and the log, see [docs/install.md](docs/install.md).

## Build LapinLibre yourself

1. Get this repository on the Linux computer.
2. Run `bash scripts/host-prereqs.sh --install` to install the packages that the build needs.
3. On Ubuntu 24.04, run `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`. The kernel build needs unprivileged user namespaces.
4. Run `./build.sh`. The first build downloads its inputs and takes a long time.
5. Run `./build.sh where`. The key is in `out/usbkey` in that location.

For the build's details and the tests, see [docs/build.md](docs/build.md).

## Use the rabbit

- The web page controls the LED, the ears, sounds, speech, tags and the webcam. See [docs/web-api.md](docs/web-api.md) for the API behind it.
- To run applications, speech and speech recognition from your own computer, start the app server and set `SERVER=` in `lapinlibre.txt`. See [docs/app-server.md](docs/app-server.md).
- The sounds the rabbit plays are listed in [docs/sound-map.md](docs/sound-map.md).

## Licence

LapinLibre's own code is under the MIT licence (`LICENSES/MIT.txt`), with these exceptions:

- `kernel/` is GPL-2.0-only (`LICENSES/GPL-2.0-only.txt`, also `kernel/COPYING`).
- `system/external/`, whose package recipes are adapted from Buildroot's, is GPL-2.0-or-later (`LICENSES/GPL-2.0-or-later.txt`).
- Patches to other projects' code (`*.patch`) are under the licence of what they patch.

Each file names its licence in an `SPDX-License-Identifier` line, and `scripts/check-repo.py` checks that it does. A few files have no such line, either because the program that reads them takes no comments, or because they are test data or recorded test output; the list and the reason for each are in `check-repo.py`. Those files are MIT too.

The built image combines this code with many other projects under their own licences: the Linux kernel, BusyBox and linphone, among others, are GPL. Some LapinLibre programs, such as `k2k-daemon` (linphone) and `multimedia-daemon` (libmpd), are GPL combined works when distributed. Downloads are pinned in `config.toml` with their sources. `image/usbkey/autorun.sig` is Violet's signed file, kept as it is: the factory system runs a stick's autorun only beside it. `image/core/rootfs/karotz/etc/gpg/` holds Karotz's own keyring (`pubring.gpg`, `trustdb.gpg`), kept as it is too; the build checks both kinds by the MD5s in `config.toml` (`[key.md5]`).
