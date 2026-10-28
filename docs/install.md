<!-- SPDX-License-Identifier: MIT -->
# Installing LapinLibre

This page tells you how to put LapinLibre on a rabbit with a USB key, and how the install works.

## How to

### Make a USB key

1. Get the key's files in one of two ways:
   - From a release: open the [releases page](https://github.com/sweetlilmre/LapinLibre/releases), download `LapinLibre-<version>.zip` and `LapinLibre-<version>.zip.sha256` from the latest release, check the download with `sha256sum -c LapinLibre-<version>.zip.sha256`, and extract the zip. Its files are the key's.
   - From your own build: build LapinLibre (see [build.md](build.md)), then run `./build.sh where` to find the work location. The key's files are in `out/usbkey` there.
2. Get an empty USB stick formatted as FAT32.
3. Copy the key's files to the root of the stick. Copy the files, not the folder.
4. Open `lapinlibre.txt` on the stick in a text editor.
5. Type the name of your Wi-Fi network after `WIFI_SSID=` and its password after `WIFI_PASSWORD=`.
6. Optional: set the other values. See [The settings file](#the-settings-file).
7. Save the file.

### Install LapinLibre on a rabbit

1. Switch off the rabbit.
2. Put the USB key in the rabbit's USB port.
3. Switch on the rabbit.
4. Wait. The nose pulses yellow and the install plays its sounds. The rabbit restarts when the install is complete.
5. Wait for the rabbit to start again. It plays the "ready" sound when it is ready.
6. Open `http://lapinlibre.local/` in a web browser. If you set `HOSTNAME`, use that name in place of `lapinlibre`.

You can leave the key in the rabbit. At the next start, the install sees that the build is already installed. It applies the settings only and the rabbit starts as usual.

### Update a rabbit to a new build

1. Make a USB key with the new release or your new build. You can leave all the values in `lapinlibre.txt` empty: the rabbit keeps its settings.
2. Do the steps in [Install LapinLibre on a rabbit](#install-lapinlibre-on-a-rabbit).

### Change a setting without a new install

1. Put the USB key with the rabbit's current build in your computer.
2. Set the value in `lapinlibre.txt` on the key. Leave the other values empty.
3. Put the key in the rabbit and switch the rabbit on.

The install applies the settings and the rabbit starts as usual. Nothing is written to the flash.

### Install the same build again

1. Connect to the rabbit with SSH: `ssh karotz@lapinlibre.local`.
2. Delete the build record: `rm /usr/etc/conf/lapinlibre_build_id`.
3. Restart the rabbit with the key in it: `reboot`.

The install then does a full install of the build on the key.

### Read the install log

1. Put the USB key in your computer.
2. Open `install-log.txt` on the key.

Each line has a time and one step of the install. An error line starts with `ERROR:`.

### What to do when the install stops with an error

The nose is solid red and the error sound plays when the install stops.

1. Read the last lines of `install-log.txt` on the key.
2. Correct the cause. For example, correct a value in `lapinlibre.txt`, or copy the key's files again.
3. Restart the rabbit with the key in it.

An error in the checks stops the install before it writes to the flash, so the rabbit keeps its old system.

## Technical details

### What a rabbit needs

The rabbit starts the key's `autorun` file at boot when the key also has `autorun.sig`. The key's `autorun` is the LapinLibre install. The install was run on a rabbit with a community firmware and on LapinLibre itself. It was not yet tried on a rabbit with its factory system.

### The key's files

| File | Contents |
|---|---|
| `autorun` | The install script. |
| `autorun.sig` | The signature file that the rabbit's boot checks before it starts `autorun`. The build copies it without changes. |
| `install-lib.sh` | The install's functions: the log, the errors, the nose, the sounds, the erase of a flash partition, the clean of `/usr`. |
| `applysettings.sh` | Checks `lapinlibre.txt` and writes its values to the rabbit's settings. |
| `install.conf` | The build id, the build version and the MD5 of `zImage`, `rootfs.img.gz` and `usr.tar.gz`. |
| `build_id` | The build id: 16 hexadecimal digits. |
| `zImage` | The Linux kernel. |
| `rootfs.img.gz` | The root filesystem. |
| `usr.tar.gz` | The contents of `/usr`: the daemons, the web page, the scripts and the default settings. |
| `install-tools.tar` | Statically linked tools for the install, for example `madplay` to play the sounds. |
| `sound/` | The install's sounds. See [sound-map.md](sound-map.md). |
| `lapinlibre.txt` | The settings file. You fill it in. |
| `install-log.txt` | The install log. The install makes it. |

### The steps of the install

1. The install starts its tools, makes the nose pulse yellow and plays `key-found.mp3`.
2. If the key has no `build_id`, the install stops and the rabbit starts as usual.
3. If `/usr/etc/conf/lapinlibre_build_id` is the same as the key's `build_id`, the install applies `lapinlibre.txt` and the rabbit starts as usual.
4. The checks: the install plays `checks-start.mp3` and `install-busy.mp3` in a loop. It copies `zImage` and `rootfs.img.gz` into RAM, and `usr.tar.gz` too when the RAM has space for it and 10 MB more. It checks the MD5 of each file, that `usr.tar.gz` decompresses, that the kernel fits `mtd1`, that the root filesystem fits `mtd2` and the RAM disk, and the values in `lapinlibre.txt`. Then it plays `checks-passed.mp3`.
5. The install plays `install-start.mp3` and the loop sound. It erases `mtd1` and writes the kernel. It erases `mtd2` and writes the root filesystem.
6. It keeps `karotz.conf` and `voice.conf` aside, cleans `/usr` and unpacks `usr.tar.gz` into `/usr`. If the unpack from the key fails, it cleans `/usr` and tries one more time. Then it puts the two files back.
7. It writes the build version to `/usr/etc/conf/sys_version` and removes files of earlier installs.
8. It applies `lapinlibre.txt` and copies `build_id` to `/usr/etc/conf/lapinlibre_build_id`.
9. It plays `install-done.mp3` and restarts the rabbit.

The install writes to the flash only after all the checks pass. An error stops the install: the nose is solid red, the loop sound stops and the error sound plays. In the checks, the error sound is `error.mp3` and then `checks-failed.mp3`. The log gets a line that starts with `ERROR:`. The log also goes to the console and to syslog with the tag `lapinlibre-install`.

### What the install keeps

The install empties `/usr` but keeps these:

| Path | Contents |
|---|---|
| `/usr/etc/conf` | The rabbit's settings. The new `/usr` has default versions of `linphone.conf`, `rfid_conf.xml` and `karotz-player.conf`, and these replace the old files. The install puts the rabbit's own `karotz.conf` and `voice.conf` back. The other files stay as they are. |
| `/usr/karotz/apps` | The applications that the platform installed. |
| `/usr/karotz/messages` | The stored messages. |
| `/usr/karotz/uuid.conf` | The rabbit's identity. |
| `/usr/lost+found` | The filesystem's own folder. |

The install does not change other files on the key, for example music folders.

### The settings file

`applysettings.sh` reads `lapinlibre.txt`. An empty value keeps the value that the rabbit has. A first install must have the Wi-Fi network, or the check fails. The file can have a byte-order mark, Windows line ends and values in quotes. An unknown setting is an error.

| Setting | Values | Written to |
|---|---|---|
| `WIFI_SSID`, `WIFI_PASSWORD` | The network name and its WPA or WPA2 password (8 to 63 characters). Set both or neither. They cannot contain `"` or `\`. | `/usr/etc/conf/wpa.conf` |
| `IP`, `NETMASK`, `GATEWAY`, `DNS` | A fixed address, for example `192.168.1.10`. Leave `IP` empty to get an address by DHCP. With `IP`, the Wi-Fi values are necessary and all four must be addresses. | `/usr/etc/conf/network.conf` |
| `SSH_PUBKEY` | One public key line: `ssh-ed25519`, `ssh-rsa` or `ecdsa-sha2-nistp…`. The rabbit accepts SSH logins with this key only. There is no password login. | `/usr/etc/conf/authorized_keys` |
| `WEB_PASSWORD` | 8 to 64 printable characters, without spaces, `"`, `\` or `:`. With a password, the web page and the API ask for the user `admin` and this password. Empty removes the password. | `/usr/etc/conf/web_password` |
| `TELNET` | `yes` or `no`. `yes` gives a root shell with no password to all on your network: use it for tests only. | `/usr/etc/conf/telnet` |
| `SERVER` | The host name or address of the computer that runs the app server. See [app-server.md](app-server.md). | `/usr/etc/conf/karotz.conf`: the platform at port 9123, the applications at `http://SERVER:8080` |
| `TTS_SERVER` | A URL like `http://192.168.1.10:8081`, if the speech server is not on `SERVER`. Default: `http://SERVER:8081`. | `/usr/etc/conf/voice.conf`, one entry for each language |
| `ASR_SERVER` | `host` or `host:port`, if the speech recognition server is not on `SERVER`. Default: `SERVER:8443`. | `/usr/etc/conf/voice.conf` |
| `LANGUAGE` | For example `en-US`, `en-GB`, `fr-FR`, `de-DE` or `es-ES`. | `/usr/etc/conf/karotz.conf` |
| `HOSTNAME` | Lower-case letters, digits and dashes. The rabbit answers to `<name>.local`. Default: `lapinlibre`. | `/usr/etc/conf/hostname` |

The install also writes `/usr/etc/conf/account.conf` when it does not exist. The controller must have an id and a password to log in to the platform, and the app server accepts all values.
