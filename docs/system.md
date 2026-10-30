<!-- SPDX-License-Identifier: MIT -->
# The rabbit's system

This page tells how the system on the rabbit works: the hardware, the flash, the boot, the daemons and their D-Bus interfaces, the controller, karotzVM and its JavaScript API, karotz-player, the settings files and the logs. For the install and `lapinlibre.txt`, see [install.md](install.md). For the web page and the HTTP API, see [web-api.md](web-api.md). For the PC-side platform server, see [app-server.md](app-server.md). For the build, see [build.md](build.md). For the sounds, see [sound-map.md](sound-map.md).

## How to

These procedures are for a person who has SSH access to the rabbit. The install gives that access when `lapinlibre.txt` has `SSH_PUBKEY` (see [install.md](install.md)). The user `karotz` has the user id 0, so it can do all the tasks below.

### Connect to the rabbit

1. Open a terminal on your computer.
2. Type `ssh karotz@lapinlibre.local`. If you gave the rabbit a different name, use `<name>.local`. You can also use the rabbit's IP address, for example `ssh karotz@192.168.1.10`.

### Read the logs

1. Connect to the rabbit.
2. To see the newest system log lines, type `tail -n 50 /var/log/messages`.
3. To follow the log while the rabbit works, type `tail -f /var/log/messages`. Push Ctrl+C to stop.
4. To see the lines of one program only, filter on its name, for example `grep Controller /var/log/messages`.
5. To see the kernel messages, type `dmesg | tail -n 50`.

The log is in RAM. It is lost when the rabbit restarts. The web API also shows the logs (see [web-api.md](web-api.md)).

### See which daemons run

1. Connect to the rabbit.
2. Type `ls /var/run/karotz`. Each `<name>.pid` file is for one daemon.
3. Type `ps | grep -e immortaldog -e daemon -e controller -e karotzVM`. Each daemon has two lines: its `immortaldog` and the daemon itself.

### Restart a daemon

immortaldog starts a daemon again when the daemon stops. Thus, to restart a daemon, stop the daemon process itself. Do not stop its immortaldog.

1. Connect to the rabbit.
2. Find the daemon's process id, for example `pidof ears-daemon`.
3. Stop the daemon: `kill $(pidof ears-daemon)`.
4. Wait 5 seconds. immortaldog starts the daemon again.
5. Make sure that it runs: `pidof ears-daemon`. The number is new.

The pid file `/var/run/karotz/<name>.pid` holds the pid of immortaldog, not the pid of the daemon. If you stop the pid in that file, immortaldog stops the daemon, removes the pid file and does not start it again.

### Restart all the core daemons

1. Connect to the rabbit.
2. Type `/etc/init.d/S40lapinlibre restart`.

This stops the controller, the button watcher, the tag listener, mdnsd and all the daemons, then starts them again in the boot order.

### Start a daemon with debug logging

The controller, ears-daemon, voice-daemon and webcam-daemon log debug messages when the environment variable `KAROTZ_DEBUG` is set.

1. Connect to the rabbit.
2. Stop the daemon and its immortaldog: `kill $(cat /var/run/karotz/controller.pid)`.
3. Start it again with the variable: `KAROTZ_DEBUG=1 /usr/karotz/bin/immortaldog /var/run/karotz/controller.pid /usr/karotz/bin/controller`.
4. Read the log: `tail -f /var/log/messages`.
5. To go back to normal logging, do steps 2 and 3 again without `KAROTZ_DEBUG=1`, or restart the rabbit.

### Call a daemon over D-Bus

Each daemon has the bus name `com.mindscape.karotz.<Name>`, the object path `/com/mindscape/karotz/<Name>` and the interface `com.mindscape.karotz.KarotzInterface`. The tables in [The daemons](#the-daemons) give the methods and their arguments.

1. Connect to the rabbit.
2. Make the nose red:

   ```sh
   dbus-send --system --print-reply --dest=com.mindscape.karotz.Led /com/mindscape/karotz/Led \
       com.mindscape.karotz.KarotzInterface.light string:"" string:FF0000
   ```

3. Make the nose pulse between green and off, every 700 ms, without end:

   ```sh
   dbus-send --system --print-reply --dest=com.mindscape.karotz.Led /com/mindscape/karotz/Led \
       com.mindscape.karotz.KarotzInterface.pulse string:"" string:00FF00 string:000000 int32:700 int32:-1
   ```

4. Move the left ear to position 5 and the right ear to position 10:

   ```sh
   dbus-send --system --print-reply --dest=com.mindscape.karotz.Ears /com/mindscape/karotz/Ears \
       com.mindscape.karotz.KarotzInterface.move string:"" int32:5 int32:30 int32:10 int32:30
   ```

5. Make the rabbit speak:

   ```sh
   dbus-send --system --print-reply --dest=com.mindscape.karotz.Voice /com/mindscape/karotz/Voice \
       com.mindscape.karotz.KarotzInterface.tts_speak string:"" string:"Hello" string:en int32:1
   ```

6. Ask the controller for its state:

   ```sh
   dbus-send --system --print-reply --dest=com.mindscape.karotz.Controller /com/mindscape/karotz/Controller \
       com.mindscape.karotz.KarotzInterface.get_status
   ```

The controller also drives the LED and the ears. A command that you send can be changed by the controller a short time later. To keep the rabbit for yourself, take control first (see the next procedure).

### Take control of the rabbit from a script

1. Take control. The controller must be in the state AVAILABLE (16):

   ```sh
   dbus-send --system --print-reply --dest=com.mindscape.karotz.Controller /com/mindscape/karotz/Controller \
       com.mindscape.karotz.KarotzInterface.take_control
   ```

2. Read the answer. `boolean true` and a string (the interactive id) tell that you have control. The nose pulses light green.
3. Send your commands to the daemons.
4. Give the control back. Put the interactive id from step 2 in the command:

   ```sh
   dbus-send --system --print-reply --dest=com.mindscape.karotz.Controller /com/mindscape/karotz/Controller \
       com.mindscape.karotz.KarotzInterface.release_control string:<interactive-id>
   ```

### Watch the D-Bus signals

1. Connect to the rabbit.
2. To see all the signals of the rabbit's daemons, type `dbus-monitor --system "type='signal',interface='com.mindscape.karotz.KarotzInterface'"`.
3. Push the head button, or put a tag on the nose. The signals show in the terminal.
4. Push Ctrl+C to stop.

### Play a sound

1. Connect to the rabbit.
2. Type `karotz-player /usr/karotz/sounds/notify.wav`.

To play through multimedia-daemon (which the controller also uses):

```sh
dbus-send --system --print-reply --dest=com.mindscape.karotz.Multimedia /com/mindscape/karotz/Multimedia \
    com.mindscape.karotz.KarotzInterface.play string:"" string:"http://<server>/stream.mp3" int32:1
```

### Pause or stop the sound that plays

1. Connect to the rabbit.
2. To pause, type `echo pause > /tmp/karotz-player.ctrl`. Do the same again to continue.
3. To stop, type `echo stop > /tmp/karotz-player.ctrl`.

Use these commands only while a sound plays through multimedia-daemon. When no player reads the FIFO, `echo` waits. Push Ctrl+C to stop it. See [karotz-player](#karotz-player).

### Add a button hook

A button hook is a program that dbus_watcher runs for each gesture of the head button.

1. Connect to the rabbit.
2. Write a script in `/etc/button.d`, for example `/etc/button.d/50log`:

   ```sh
   #!/bin/sh
   # $1 is the gesture (click, dclick, tclick, mclick, lclick_start, lclick_stop), $2 the clicks.
   logger -t my-hook "gesture $1, clicks $2"
   ```

3. Make it executable: `chmod 755 /etc/button.d/50log`.
4. Push the head button once.
5. Read the log: `grep my-hook /var/log/messages`.

A hook has 10 seconds at most. See [The button watcher](#the-button-watcher-dbus_watcher).

### Write a small application

An application is a folder with a `descriptor.xml` and a `main.js`. The app server gives it to the rabbit (see [app-server.md](app-server.md)).

1. On your computer, make a folder in the app server's applications folder. Its name is the application's API key, for example `myapp`.
2. Write `descriptor.xml` in it:

   ```xml
   <descriptor>
     <version>1</version>
     <name>myapp</name>
     <deployment>hosted</deployment>
     <accesses><access>led</access><access>ears</access><access>tts</access></accesses>
   </descriptor>
   ```

3. Write `main.js` in it:

   ```js
   var spoken = false;
   karotz.connect("127.0.0.1", 9123);
   karotz.start(function (data) {
       karotz.led.light("00FF00");
       karotz.ears.move(5, 5, function (event) {
           if (event == "OK" || spoken) return;
           spoken = true;
           karotz.tts.start("Hello from " + instanceName, "en-US", function (event) {
               if (event != "OK") exit();
           });
       });
   }, {});
   ```

   Each callback runs first with `OK` (the controller accepted the command), then again when the job ends (`TERMINATED`, `CANCELLED` or `ERROR`). Call `exit()` only at the end. The application must call `karotz.start` and get control within 10 seconds, or karotzVM stops it.

4. Give the application an instance with a trigger (a tag, a speech command or a schedule) in the app server's settings, and send the settings to the rabbit (see [app-server.md](app-server.md)).
5. Start the application with its trigger, or with `appctl.py run`.
6. Read the log on the rabbit: `grep KarotzVM /var/log/messages`. The `log("text")` lines show as `JSLOG: text`.

The JavaScript API is in [The JavaScript API](#the-javascript-api).

## Technical details

This section is the reference. It tells how each part works, with its files, devices, interfaces and protocols.

### The hardware

The rabbit has a Samsung S3C2440A processor (an ARM920T core, without a floating-point unit), 64 MB of SDRAM and 256 MB of NAND flash. The kernel is Linux 2.6.32.71 with the board file `kernel/src/arch/arm/mach-s3c2440/mach-rabbitv3.c` (machine RABBITV3, number 1912) and the configuration `system/kernel/rabbitv3_defconfig`. The table gives each part, how it connects, its driver and its device node.

| Part | Connection | Driver | Device node | Used by |
|---|---|---|---|---|
| Nose LED | SPI0, chip select 1 (GPG2), 10 MHz | `spidev` | `/dev/spidev0.1` | led-daemon, the `led` tool |
| Ears (two motors with encoders) | Left: PWM0 on TOUT0, direction GPF4, encoder EINT1. Right: PWM1 on TOUT1, direction GPF5, encoder EINT2 | `rabbit_ears` (`kernel/src/drivers/char/rabbit_ears.c`), character major 60 | `/dev/rabbit_ears` | ears-daemon, the `ears` tool |
| Head button | GPF0 | `rabbit_button` (`rabbit_button.c`), character major 52 | `/dev/rabbit_button` | button-daemon |
| Power switch | GPC2 | `rabbit_poweron` (`rabbit_poweron.c`), character major 53 | `/dev/rabbit_power` | the `power` tool |
| RFID reader | I2C, 400 kHz, slave address 1 | `i2c-dev` | `/dev/i2c-0` | rfid-daemon |
| Speaker and microphone | UDA1341 codec on I2S, its L3 bus on GPB2-4 | ASoC `s3c24xx_uda134x`, with OSS emulation | `/dev/dsp` (play), `/dev/audio` (record) | karotz-player, multimedia-daemon, voice-daemon, k2k-daemon |
| Camera | OV7675 sensor on the camera interface, SCCB on GPG3, GPG5, GPG7 | `s3c2440camif` (`kernel/src/drivers/media/video/s3c2440camif.c`), misc device | `/dev/camera` | webcam-daemon, controller |
| Wi-Fi | USB | `rt73usb`, firmware `rt73.bin` from `/lib/firmware` | `wlan0` | `network.sh` |
| USB stick | USB host (OHCI) | `ub` | `/dev/uba`, `/dev/uba1`, `/dev/ubb`, `/dev/ubb1` | `usbkey.sh` |
| Serial ports | UART0-2, 8N1 | `s3c2410` serial | `/dev/s3c2410_serial0` to `2` (links `/dev/ttyS0` to `2`) | serial-daemon |
| USB device port | USB gadget, D+ pull-up on GPG11 | `g_serial` | `/dev/ttyGS0` | serial-daemon |
| Clock | On the processor | `rtc-s3c` | `/dev/rtc0` (link `/dev/rtc`) | the system |
| Power inputs | ADC AIN0 and AIN1 | `s3c24xx-hwmon`, raw values | sysfs | none known |

The drivers of the rabbit's own parts work as follows:

- `rabbit_button`: each press and each release causes an interrupt. The driver puts `1` (pressed) or `0` (released) in a 16-byte queue. `read()` gives the queued bytes and does not block. `poll()` tells when bytes are there.
- `rabbit_ears`: `ioctl` type `E`. Command 1 moves the right ear and command 2 the left ear, with `struct { int pwm; int pos; }`. Command 3 reads the step counters (`{ right, left }`), or writes 0 into a counter to clear it. Command 4 reads or clears the status bits: 1 left moving, 2 right moving, 4 left moved without power or blocked, 8 the same for the right ear. Each encoder step puts one byte in the queue for `read()`: the time since the last step in units of 20 ms (127 at most), with bit 7 set for the left ear.
- `rabbit_poweron`: `read()` gives one byte, `1` when GPC2 is low (power on) and `0` when it is high.
- `s3c2440camif`: a misc device named `camera` with two image buffers. A private `ioctl` sets VGA (640x480) or QVGA (320x240), RGB565.

The board file also declares the head button as a `gpio-keys` input device. The kernel configuration does not build the `gpio-keys` driver, so this input device does not exist.

### The flash

The NAND flash has eight partitions, set in the board file. The flash has no ECC, because the bootloader writes it without ECC.

| mtd | Name | Offset | Size | Contents |
|---|---|---|---|---|
| 0 | `bootloader` | 0x0000000 | 256 KiB | The bootloader. Locked. |
| 1 | `Kernel` | 0x0040000 | 3 MiB | The kernel (`zImage`). `/dev/kernel` is a link to `mtdblock1`. |
| 2 | `ramdisk` | 0x0340000 | 5 MiB | The root file system: a gzipped cramfs image. |
| 3 | `kernel rescue` | 0x0840000 | 3 MiB | The rescue kernel. Locked. |
| 4 | `ramdisk rescue` | 0x0C40000 | 5 MiB | The rescue root file system. Locked. |
| 5 | `yaffs rescue` | 0x1240000 | 3 MiB | The rescue data. |
| 6 | `yaffs` | 0x1540000 | 234 MiB | `/usr`, a YAFFS file system: the daemons, the settings, the applications, the sounds. |
| 7 | `mystery box` | 0xFF40000 | 768 KiB | The rest of the chip. Its use is not known. |

There is an unused 1 MiB gap after `kernel rescue` and another after `ramdisk rescue`. `config.toml` `[limits]` gives the size limits for the build: the kernel 3145728 bytes, the gzipped root file system 5242880 bytes, and the unpacked root file system 5242880 bytes. The bootloader loads mtd2 as a gzipped initrd into the RAM disk `ram0` (`initrd=0x31800000,0x00500000`, 5 MiB), so the unpacked root file system must also fit 5 MiB.

### The file systems

| Mount point | What | Writable | Contents |
|---|---|---|---|
| `/` | cramfs in `ram0`, from mtd2 | No | BusyBox, the libraries, `/karotz/bin` (`led`, `ears`, `power`), `/karotz/scripts`, `/karotz/etc` |
| `/usr` | YAFFS on `/dev/mtdblock6` | Yes | `/usr/karotz/bin` (the daemons), `/usr/etc`, `/usr/karotz/apps`, `/usr/karotz/sounds`, `/usr/scripts`, `/usr/www` |
| `/etc` | A link to `/usr/etc` | Yes | The settings (`/etc/conf` is `/usr/etc/conf`) |
| `/var` | ramfs | Yes, in RAM | `/var/run/karotz` (pid files), `/var/log` (the log), `/var/lib/dbus` |
| `/dev` | tmpfs | Yes, in RAM | The device nodes |
| `/tmp` | A link to `/var/tmp` | Yes, in RAM | `/tmp/karotz-player.ctrl`, `/tmp/apps.abnf`, `/tmp/account.conf`, `/tmp/picture.jpg`, `/tmp/record.flac` |
| `/mnt/usbkey` | vfat on the USB stick | Yes | The stick, mounted by `usbkey.sh` |

`/dev/pts` (devpts), `/dev/shm` (tmpfs) and `/proc/bus/usb` (usbfs) come from `/karotz/etc/fstab`.

### The boot

The boot has these steps:

1. The bootloader loads the kernel from mtd1 and the root file system from mtd2 into `ram0`. It starts the kernel with `init=/linuxrc`.
2. `/linuxrc` (`image/core/rootfs/linuxrc`) mounts `/proc`, `/sys`, `/var` (ramfs) and `/dev` (tmpfs). It makes the directories in `/var`, among them `/var/run/karotz`.
3. `linuxrc` makes the device nodes: the console, the USB stick (`uba`, `uba1`, `ubb`, `ubb1`), `rtc0`, `rabbit_ears`, `rabbit_button`, `rabbit_power` and the serial ports.
4. `linuxrc` sets `/sbin/mdev` as the hotplug helper and runs `mdev -s`. This makes the nodes of the devices that are already there, the flash partitions among them.
5. `linuxrc` mounts `/usr` (YAFFS, `/dev/mtdblock6`).
6. `linuxrc` makes `/etc/fstab`, `group`, `hosts`, `ld.so.conf`, `profile`, `motd`, `shells`, `mdev.conf` and `busybox.conf` links to the files in `/karotz/etc`. Thus a new install always uses its own copies. It copies `passwd`, `inittab` and `inetd.conf` to `/usr/etc` only when they are not there, so changes on the rabbit stay.
7. `linuxrc` runs `mdev -s` again, now with the rules of `mdev.conf`. A USB stick that is in at power-on is mounted on `/mnt/usbkey` now. `null`, `zero`, `full`, `random`, `urandom`, `ptmx` and `tty` stay mode 0666 for every user; other devices without a rule get mdev's default, root's and 0660.
8. `linuxrc` sets the host name from `/usr/etc/conf/hostname` (default `lapinlibre`), runs `ldconfig` if there is no `/etc/ld.so.cache`, runs `mount -a` and starts `syslogd`. It logs `[START] LapinLibre <build>`.
9. If `/mnt/usbkey/autorun` exists, `linuxrc` runs it with bash. This is the USB key's install (see [install.md](install.md)). It can write the flash and restart the rabbit.
10. `linuxrc` starts BusyBox `init`.
11. `init` reads `/etc/inittab`. It runs `/karotz/scripts/startup.sh` once. It keeps `/karotz/bin/power`, `/karotz/scripts/sshd.sh` and `/karotz/scripts/inetd.sh` running.
12. `startup.sh` writes `UTC0` to `/etc/TZ` if the file does not exist, and starts `crond` with `/usr/spool/cron/crontabs`.
13. `startup.sh` makes the nose pulse cyan with the `led` tool and runs `network.sh 60`. `network.sh` starts `wpa_supplicant` with `/usr/etc/conf/wpa.conf`. It sets a fixed address from `/usr/etc/conf/network.conf`, or starts `udhcpc`. It waits at most 60 seconds for an access point and an IPv4 address.
14. If the network does not come up, the nose is red. `startup.sh` tries `network.sh` again every 30 seconds in the background, and continues with the next step when the network is up.
15. `startup.sh` runs each executable `/etc/init.d/S??*` with `start`, in name order: `S30dbus-daemon` (the D-Bus system bus), then `S40lapinlibre` (the core daemons), then any later scripts.
16. `S40lapinlibre` makes `/usr/karotz/uuid.conf` with `uuidgen` if it is empty or missing. The first time only, it makes `/usr/etc/conf/playsounds` (and the marker `playsounds.default`).
17. `S40lapinlibre` starts each daemon under immortaldog, in this order: led, rfid, webcam, button, ears, multimedia, voice, scheduler, serial, k2k. Then it starts `/usr/scripts/dbus_watcher` and `/usr/scripts/tag_listener`, each under immortaldog and only when no live process holds its pid file. The tag listener runs as the user `web` (`setuidgid web`), like the web API; `/karotz/scripts/web-files.sh` makes its files first (see [The web user](#the-web-user)).
18. `S40lapinlibre` waits at most 15 seconds until the bus names `com.mindscape.karotz.Led`, `Ears`, `Multimedia`, `Voice`, `Scheduler`, `Rfid` and `Button` have an owner. If some are missing, it logs `starting the controller without: <names>`. Then it starts the controller under immortaldog.
19. `S40lapinlibre` starts `mdnsd -i wlan0`. The rabbit answers to `<name>.local` and advertises the services in `/etc/mdns.d` (SSH).
20. `startup.sh` waits at most 30 seconds for `com.mindscape.karotz.Led`. Then it stops the `led` tool, and led-daemon pulses the nose cyan. From now on only led-daemon sets the nose.
21. `startup.sh` starts `ntpd -n -p pool.ntp.org` under immortaldog (pid file `/var/run/karotz/ntpd.pid`). ntpd keeps the clock set for as long as the rabbit runs, and tries again until a time server answers. `startup.sh` waits at most 30 seconds until the clock is set (a time after 2026-01-01), and logs `the clock is set` or `the clock isn't set yet: ntpd keeps trying pool.ntp.org`.
22. `startup.sh` tells led-daemon to pulse the nose green, plays `/usr/karotz/sounds/boot-ready.wav` and logs `end`.

The controller then connects to the platform and sets the nose and the ears (see [The controller](#the-controller)).

At shutdown, `init` lights the nose red with the `led` tool, stops `wlan0` and remounts the file systems read-only.

`inittab` lines that `init` keeps running:

| Program | What it does |
|---|---|
| `/karotz/bin/power` | Reads `/dev/rabbit_power` once a second. When it reads `0` (the switch is off), it runs `reboot`. |
| `/karotz/scripts/sshd.sh` | Runs Dropbear on port 22, public-key login only. Without `/usr/karotz/.ssh/authorized_keys` it waits and does not start Dropbear. The host keys are in `/usr/etc/conf/dropbear`; it makes the missing ones (RSA, ECDSA, Ed25519) before Dropbear starts. At each start it writes the keys' public parts for the web API: `/var/run/ssh/authorized_keys` (a copy) and `/var/run/ssh/host_keys` (each host key's public line). |
| `/karotz/scripts/inetd.sh` | Runs `web-files.sh`, writes the web server's configuration in `/var/run` (root's, readable by the group `web`), then runs `inetd` (the web page, as the user `web`; telnet when `/usr/etc/conf/telnet` exists). See [web-api.md](web-api.md). |

### The USB stick

mdev runs `/karotz/scripts/usbkey.sh` for each `ub` disk or partition. On `add`, the script mounts the first partition (or the whole disk if it has no partition table) as vfat on `/mnt/usbkey`, only once. On `remove`, it unmounts it. The hooks in `/usr/etc/usbkey.d` run with `add` after the mount, with `remove` before the unmount and with `removed` after it. Each hook has 10 seconds at most. The core has one hook, `50mpd`, which makes mpd read the stick's music again.

### The web user

The web server (`httpd`, its CGI programs) and the tag listener run as the user `web` (uid and gid 80, no login), not as root. A fault in one of them thus does not give root.

| What | How |
|---|---|
| `httpd` | `inetd.conf`'s user field is `web`. inetd (root) keeps port 80. |
| The tag listener | `S40lapinlibre` starts it with `setuidgid web`. |
| Its files | `/karotz/scripts/web-files.sh` (root, run by `inetd.sh` and `S40lapinlibre`) makes `/var/run/lapinlibre-web` and `/usr/etc/conf/web` (owner `web`, mode 0700) and the locks `/var/lock/lapinlibre-web.lock` and `/var/lock/lapinlibre-tags.lock` (owner `web`, 0600). A data folder that an earlier build left to root is given to `web`. |
| httpd's configuration | `/var/run/httpd.conf` is root's, mode 0640, group `web`: `web` can read the password hash but not change it. |
| D-Bus | `karotz.conf` lets the group `web` call the rabbit's services. Signal watches (`dbus-monitor`) work for any user. |
| reboot | `/bin/busybox` is setuid root. `/etc/busybox.conf` (from `/karotz/etc`, read-only) lets only the applet `reboot` keep root, for root and the group `web`. Every other applet runs as its caller, the applets that BusyBox marks as needing setuid (`mount`, `su`, `passwd`, `login` and others) among them. |
| The clock | ntpd (root, started by `startup.sh`) keeps it set. `setclock` only waits for it. |
| The SSH keys | `sshd.sh` (root) writes the public parts to `/var/run/ssh`. The key files stay root's. |
| The camera's picture | webcam-daemon (root) writes `/tmp/picture.jpg`. `/tmp` stays root's (0755): `web` reads the picture and copies it. A `/tmp` that `web` could write would let it put a link where root writes. |
| The nose | Only led-daemon: `startup.sh` stops its `led` tool as soon as led-daemon is on the bus. |

### immortaldog

immortaldog keeps one program running.

```
immortaldog <path_to_pidfile> <path_to_executable> [<argument>...]
```

- The arguments after the program are passed to it, at each start (`S40lapinlibre` starts the tag listener as `/bin/busybox setuidgid web /usr/scripts/tag_listener`, `startup.sh` starts `/bin/busybox ntpd -n -p pool.ntp.org`).
- It goes to the background (a new session, the working directory `/`), and closes its standard input.
- If the pid file exists, it logs `daemon seems running` and stops. Otherwise it writes its own pid in the pid file.
- It starts the program. When the program stops, it starts it again. There are at least 5 seconds between two starts.
- On SIGTERM it sends SIGTERM to the program, removes the pid file and stops.
- If the program cannot be started, immortaldog stops and removes the pid file.
- It accepts no arguments for the program.
- It logs to syslog with the name `ImmortalDog` (facility local1).

The pid files are in `/var/run/karotz`: `led.pid`, `rfid.pid`, `webcam.pid`, `button.pid`, `ears.pid`, `multimedia.pid`, `voice.pid`, `scheduler.pid`, `serial.pid`, `k2k.pid`, `dbus_watcher.pid`, `tag_listener.pid` and `controller.pid`. Each holds the pid of immortaldog, not the pid of the program.

To restart one program, stop the program's own process (`kill $(pidof <program>)`). To stop it for good, stop its immortaldog (`kill $(cat /var/run/karotz/<name>.pid)`).

### D-Bus

All the daemons are on the D-Bus system bus. Each daemon has the bus name `com.mindscape.karotz.<Name>`, the object path `/com/mindscape/karotz/<Name>` and the interface `com.mindscape.karotz.KarotzInterface`. These are the names of the Karotz's own system, kept so that scripts written for it (`dbus-send`, `dbus-monitor`) work unchanged. `/usr/etc/dbus-1/system.d/karotz.conf` lets root own any name that starts with `com.mindscape.karotz` and send to these names. It lets the group `web` (the web API and the tag listener) send to these names, but own none. The interfaces are in `userland/dbus/*.xml`. The XML files list only the names of the signals: the signal arguments below come from each daemon's `g_signal_new()`.

Each daemon emits `registered` (no arguments) when it has its name on the bus. The programs use the helpers in `libkarotz-common` (`userland/src/usr/libkarotz-common/karotz-common.h`) to call the daemons. These helpers also know the names `com.mindscape.karotz.Choreography_manager` and `com.mindscape.karotz.Dock`, but no program on the rabbit owns them.

Many methods take an `id` as their first argument. The daemon gives this id back in its end signal, so that the caller can match the signal to its request. An empty id is a request that nobody waits for. Many methods also take `behaviour`; the daemons do not use it.

Many end signals have a code. These codes are the `ReturnCode` values of the platform protocol: 4 is `TERMINATED` (done) and 5 is `CANCELLED` (stopped before the end).

### The daemons

The daemons are in `/usr/karotz/bin`. Each logs to syslog with its own name (facility local1), at the levels notice and up. The table gives a summary.

| Daemon | Bus name | Device | Log name |
|---|---|---|---|
| led-daemon | `com.mindscape.karotz.Led` | `/dev/spidev0.1` | `Led` |
| ears-daemon | `com.mindscape.karotz.Ears` | `/dev/rabbit_ears` | `Ears` |
| button-daemon | `com.mindscape.karotz.Button` | `/dev/rabbit_button` | `Button` |
| rfid-daemon | `com.mindscape.karotz.Rfid` | `/dev/i2c-0` | `Rfid` |
| multimedia-daemon | `com.mindscape.karotz.Multimedia` | `/dev/audio`; plays with karotz-player and mpd | `Multimedia` |
| voice-daemon | `com.mindscape.karotz.Voice` | `/dev/audio`; plays with `madplay` | `Voice` |
| webcam-daemon | `com.mindscape.karotz.Webcam` | `/dev/camera` | `Webcam` |
| scheduler-daemon | `com.mindscape.karotz.Scheduler` | none | `Scheduler` |
| serial-daemon | `com.mindscape.karotz.Serial` | the serial ports | none (it does not open syslog) |
| k2k-daemon | `com.mindscape.karotz.K2k` | the audio devices, through liblinphone | `K2k` |
| controller | `com.mindscape.karotz.Controller` | `/dev/camera` for its own snapshots | `Controller` |

#### led-daemon

led-daemon drives the nose LED through the SPI LED driver chip. Colours are `RRGGBB`. Animations run on a 30 ms timer. A new command stops the animation that runs.

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `light` | `id` s, `color` s | `result` b | Sets the colour at once. |
| `fade` | `id` s, `color` s, `period` i | `result` b | Fades from the current colour to `color` in `period` ms. |
| `pulse` | `id` s, `color1` s, `color2` s, `period` i, `length` i | `result` b | Goes between the two colours every `period` ms, for `length` ms. A negative `length` pulses without end. |

| Signal | Arguments | When |
|---|---|---|
| `event` | `id` s, `code` i | 4 when an animation ends or `light` succeeds. 5 when an animation is replaced, or for a bad colour or an SPI error. |

The controller sends the colour `n` as `color1` of a pulse. led-daemon does not accept it as a colour and uses the current colour.

#### ears-daemon

ears-daemon drives the two ear motors. Every 60 ms it reads the driver, runs the requests that wait, and watches the position of each ear. The methods only put a request in a queue. There are three request channels: the right ear, the left ear and both ears. Speeds are 1 to 255. A position is a step count of the encoder. A reset turns the ears backwards until they find the gap in the encoder, which is position 0.

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `reset` | `id` s, `ears` s | `result` b | Resets the ears: `L` the left one, `R` the right one, any other value both. |
| `move` | `id` s, `l_position` i, `l_speed` i, `r_position` i, `r_speed` i | `result` b | Moves both ears to the positions. |
| `move_left` | `id` s, `l_position` i, `l_speed` i | `result` b | Moves the left ear to the position. |
| `move_right` | `id` s, `r_position` i, `r_speed` i | `result` b | Moves the right ear to the position. |
| `move_step` | `id` s, `l_position` i, `l_speed` i, `r_position` i, `r_speed` i | `result` b | Moves both ears by the given numbers of steps. |
| `move_left_step` | `id` s, `l_position` i, `l_speed` i | `result` b | Moves the left ear by a number of steps. |
| `move_right_step` | `id` s, `r_position` i, `r_speed` i | `result` b | Moves the right ear by a number of steps. |

| Signal | Arguments | When |
|---|---|---|
| `event` | `id` s, `code` i | 4 when a move is done. 5 when a move is interrupted or blocked. |
| `reset_finished` | `id` s, `right` i, `left` i | A reset is done. The numbers are the counters when each ear passed its gap. |
| `move_left_start`, `move_right_start` | none | An ear starts to move. |
| `move_left_stop`, `move_right_stop` | `steps` i, `ms` i | An ear stops: the steps since it started, and the time it moved. |
| `move_left_blocked`, `move_right_blocked` | none | An ear is blocked. |

#### button-daemon

button-daemon reads the head button and emits a signal for each gesture. A level counts only when it stays for 50 ms with no other byte, because the contacts bounce. Presses less than 300 ms apart are counted together. A press held for 450 ms is a long press.

button-daemon has no methods.

| Signal | Arguments | Gesture |
|---|---|---|
| `click` | none | One press. |
| `dclick` | none | Two presses. |
| `tclick` | none | Three presses. |
| `mclick` | `count` i | Four presses or more. |
| `lclick_start` | none | The button is held for 450 ms. |
| `lclick_stop` | none | The held button is released. |

#### rfid-daemon

rfid-daemon reads the RFID tags on the nose. The reader is a contactless coupler on `/dev/i2c-0` (slave address 1), in ISO 14443-B mode, for ST SRI512 and SRIX tags. Every 100 ms the daemon looks for tags. For a new tag it reads the UID, the 16 blocks and the system block. It uses anti-collision when several tags answer.

`/etc/conf/rfid_conf.xml` can tell the daemon to write block 7 (the product codes) into each new tag. The default is `writeInTag="false"`: the rabbit only reads tags.

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `getTags` | none | `tags` s, `result` b | Gives `;uid:blocks` for each tag on the reader. |

| Signal | Arguments | When |
|---|---|---|
| `tag_in` | `uid` s, `blocks` s, `app_code` i, `type_code` i, `pict_code` i, `color_code` i | A tag comes on the nose. |
| `tag_out` | `uid` s | A tag stops answering. |

#### multimedia-daemon

multimedia-daemon plays music and records from the microphone. It has two back ends:

- karotz-player, for paths and URLs. The first `play` starts `karotz-player -include /etc/conf/karotz-player.conf <url>` (with `-playlist` for `.asx`, `.m3u` and `.pls`). The next plays, `pause` and `resume` are commands written to the FIFO `/tmp/karotz-player.ctrl`. `stop` stops the player process.
- mpd, for URLs of the form `command::argument`: `playlist::`, `title::`, `dir::`, `artist::`, `genre::`, `allsong::`, and the settings `mode::` (`normal`, `random`, `repeat`, `norepeat`) and `lock::` (`yes` or `no`). The daemon connects to mpd on 127.0.0.1:6600 through libmpd. It starts `mpd /etc/mpd/mpd.conf` if it cannot connect, and checks the connection every second.

`record` reads 16 kHz mono from `/dev/audio`, and sends it to `flac` (`/tmp/record.flac`), for 90 seconds at most.

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `play` | `id` s, `url` s, `behaviour` i | `result` b | Plays a file, a URL, a playlist or an mpd command. |
| `pause` | `id` s, `behaviour` i | `result` b | Pauses. |
| `resume` | `id` s, `behaviour` i | `result` b | Continues after a pause. |
| `stop` | `id` s, `behaviour` i | `result` b | Stops. |
| `next` | `id` s, `behaviour` i | `result` b | The next item (mpd). |
| `previous` | `id` s, `behaviour` i | `result` b | The previous item (mpd). |
| `record` | `id` s, `url` s, `behaviour` i | `result` b | Records from the microphone. |
| `get_artist` | none | `artistList` s, `result` b | The artists of the mpd database (`a:b:c:`). |
| `get_folder` | none | `folderList` s, `result` b | The folders. |
| `get_playlist` | none | `playlistList` s, `result` b | The playlists. |
| `get_genre` | none | `genreList` s, `result` b | The genres. |
| `get_song` | none | `songList` s, `result` b | The songs. |

| Signal | Arguments | When |
|---|---|---|
| `multi_playfinished` | `id` s, `code` i | 4 on stop or pause. |
| `multi_recordfinished` | `id` s, `code` i | 4 when a recording ends. |
| `multi_callback` | `action` i | The playback changes: 1 play, 2 stop, 3 pause, 5 the mpd database changed, 6 another mpd client connected. |

#### voice-daemon

voice-daemon speaks text (TTS) and recognises speech (ASR). It reads `/usr/etc/conf/voice.conf`.

- TTS: `tts_speak` starts a child process. The child sends the text with an HTTP POST to the server for the language in `[tts]` (the key is the first two letters of `lang`, in lower case). It sends the answer (MP3) to `madplay -a 1.5 -`. Only one text is spoken at a time. The connection to the speech server must be made within 3 s; until it fails, the rabbit counts as speaking.
- ASR: `asr_start_string` and `asr_start_file` connect over TCP to a server in `[asr]` (`server0` and up, `nbserver`, `port`). The first server is chosen at random, the next ones in turn when a connection fails. The daemon sends the grammar, then the microphone (`/dev/audio`, mu-law, 16 kHz, mono) until the server answers. Each message on the socket is a 4-byte big-endian length and a `lapinlibre.asr.AsrMsg` (`userland/proto/asr-message.proto`). A connection to a TTS or an ASR server has a time limit.

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `tts_speak` | `id` s, `text` s, `lang` s, `behaviour` i | `result` b | Speaks the text. |
| `tts_stop` | `id` s, `behaviour` i | `result` b | Stops the speech. |
| `asr_start_string` | `uid` s, `grammar` s, `lang` s, `behaviour` i | `result` b | Starts recognition with a grammar. |
| `asr_start_file` | `uid` s, `filename` s, `behaviour` i | `result` b | Starts recognition with a grammar file. |
| `asr_stop` | `id` s, `behaviour` i | `result` b | Stops the recognition, with no signal. |

| Signal | Arguments | When |
|---|---|---|
| `tts_event` | `id` s, `code` i | 4 when the speech ends or is stopped; 2 when it failed (the speech couldn't be fetched from the server, or curl or madplay couldn't be started). |
| `asr_event` | `uid` s, `words` s, `semantic` s, `score` i | The result. `words` is `<nomatch>` when the server recognised nothing. `asr_event(uid, "<error_server_timeout>", "cancel", 2)` tells that the session could not start. |

#### webcam-daemon

webcam-daemon takes a photo and sends it. `takeSnapshot` opens `/dev/camera`, sets VGA (640x480, RGB565), pulses the LED, plays the shutter sound, takes three frames and makes a JPEG of the last one (quality 80). If `snapURL` is `file`, it writes `/tmp/picture.jpg`. Otherwise it sends the JPEG to `snapURL` as a multipart form POST. It closes the camera after each photo.

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `takeSnapshot` | `id` s, `snapURL` s, `behaviour` i | `result` b | Takes a photo. `id` is `interactiveid:correlationid`. |

| Signal | Arguments | When |
|---|---|---|
| `event` | `correlationid` s, `code` i | 4 when the photo is sent. |

#### scheduler-daemon

scheduler-daemon is an alarm clock. Each entry has a name (`idScheduler`). A single GLib timeout, set for the earliest entry, drives all the entries.

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `add_date_entry` | `idScheduler` s, `year` i, `month` i, `day` i, `hour` i, `minute` i, `second` i | `result` b | A date and a time. A value out of range in `year`, `month`, `day` or `hour` matches all values, so the entry repeats each year, month, day or hour. |
| `add_weekly_entry` | `idScheduler` s, `week` i, `hour` i, `minute` i, `second` i | `result` b | A day of the week (0 is Sunday) and a time. |
| `add_periodic_entry` | `idScheduler` s, `period` i, `delta` i | `result` b | Every `period` seconds, plus a random 0 to `delta`-1 seconds when `delta` > 0. |
| `delete_entry` | `idScheduler` s | `result` b | Removes one entry. |
| `delete_all_entry` | none | `result` b | Removes all the entries. |
| `print_task` | none | none | Writes the entries to the log. |

| Signal | Arguments | When |
|---|---|---|
| `schedule_event` | `id` s, `more` i | An entry is due. `more` is true when the entry stays, false when it was a one-time entry that is now removed. |

#### serial-daemon

serial-daemon gives access to the serial ports. `open` opens a port (8N1, no flow control) and keeps it by name. Each received line (up to `\n`, without it) is sent as a signal, as text or as upper-case hex. Before it opens `/dev/ttyGS0` (the USB device port), the daemon stops `miniusb_read.sh` if it runs.

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `ping` | none | `result` b | Tells that the daemon runs. |
| `open` | `port` s, `baud` i | `result` b | Opens a port. |
| `close` | `port` s | `result` b | Closes a port, or all ports for an empty name. |
| `write` | `port` s, `data` s, `base` s | `result` b | Sends `data` as text when `base` starts with `a`, else as hex digits. |
| `readlinemode` | `port` s, `base` s | `result` b | Sets the form of the `readline` signal: text or hex. |

| Signal | Arguments | When |
|---|---|---|
| `readline` | `port` s, `line` s | A line came in on a port. |

#### k2k-daemon

k2k-daemon makes voice calls between rabbits over SIP, with liblinphone. At start it registers the SIP account: `sipDomain` and `stunServer` from `[k2k]` in `/etc/conf/karotz.conf`, and `id` and `pwd` from `[account]` in `/etc/conf/account.conf`. The linphone settings are in `/etc/conf/linphone.conf`. The rabbit shows as busy unless the controller's state is AVAILABLE. `--debug` writes linphone's log to `/tmp/linphone.log`.

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `k2k_call` | `sipAddr` s | `result` b | Calls a SIP address. |
| `k2k_answer` | `ans` b | `result` b | Accepts or refuses the incoming call. |
| `k2k_hangup` | none | `result` b | Ends the call. |
| `k2k_set_busy` | `busy` b | `result` b | Sets the busy state. |
| `k2k_set_answering_machine` | `on` b | `result` b | Not implemented: the value is ignored (logged as a warning) and the result is true. |
| `k2k_use_account` | `sipId` s, `sipDomain` s, `sipPassword` s, `stunServer` s | `result` b | Replaces the SIP account. |
| `k2k_echo_calibration` | none | `result` b | Starts the echo calibration. |

| Signal | Arguments | When |
|---|---|---|
| `incoming_call` | `from` s | A call comes in. |
| `beginning_call` | `with` s | A call starts. |
| `beginning_outgoing_call` | `with` s | An outgoing call starts. |
| `finished_call` | `with` s | A call ends. |

### The button watcher (dbus_watcher)

`/usr/scripts/dbus_watcher` is a shell script that immortaldog keeps running. It reads button-daemon's signals with `dbus-monitor --system`, filtered on the path `/com/mindscape/karotz/Button`. For each gesture it does two things:

- It plays a short tone from `/usr/karotz/sounds` with karotz-player, only when `/etc/conf/playsounds` exists: `button-click.wav` for `click`, `button-double.wav` for `dclick`, `button-triple.wav` for `tclick`, `button-long.wav` for `lclick_start`. Each tone has 5 seconds at most. A new tone stops the tone that plays.
- It runs the hooks in `/etc/button.d`: each executable regular file, in name order, but not names that start with `.` or end with `~`. `$1` is the gesture (the signal name) and `$2` the number of clicks (1, 2, 3, the count for `mclick`, 0 for a long press). The environment variables `BUTTON_GESTURE` and `BUTTON_CLICKS` have the same values. Standard input is `/dev/null`. Each hook has 10 seconds at most. The output goes to syslog. At most 4 groups of hooks run at the same time; a gesture that finds 4 still running gets no hooks.

The watcher does not act on the rabbit. The controller acts on the button itself. When the bus stops, the watcher stops with status 1, and immortaldog starts it again.

### The tag listener (tag_listener)

`/usr/scripts/tag_listener` is a shell script that immortaldog keeps running. It reads rfid-daemon's `tag_in` signals. It does the web API's tag learning and gives every tag's feedback: the LED red as soon as the tag is read, then for a stored tag with an action a tone and the action (a speech action is logged once voice-daemon has played it or failed, and a failure makes the LED pulse red while the error sound plays); for an application's tag the same tone, then the controller's `start_tag_app`; for a stored tag without an action or an unknown tag, the error sound and the language's phrase. See [web-api.md](web-api.md).

### The controller

The controller (`/usr/karotz/bin/controller`, bus name `com.mindscape.karotz.Controller`) is the centre of the system. It reads `/usr/etc/conf/karotz.conf` and the language file `/usr/karotz/res/lang/<lang>` (en-US when the file is missing; en-US also gives each key that the language file does not have). It keeps the rabbit's state, connects to the platform, handles the protocol messages, listens to the daemons' signals, and runs the applications in karotzVM.

#### Its D-Bus interface

| Method | Arguments (in) | Out | What it does |
|---|---|---|---|
| `get_status` | none | `karotz` i, `network` i, `platform` i | The three states (see the table below). |
| `take_control` | none | `success` b, `interactive_id` s | When the state is AVAILABLE, it starts interactive mode for a new id. Otherwise `success` is false and the id is empty. |
| `release_control` | `interactive_id` s | none | Stops interactive mode for this id. |
| `has_tag_app` | `id` s | `owned` b | True when the controller is logged in and AVAILABLE and an application's instance has this tag. The tag listener asks it. |
| `start_tag_app` | `id` s, `content` s, `app` i, `type` i, `pict` i, `color` i | `started` b | Starts the application of the tag's instance (see "Triggers" below). False when not AVAILABLE, when no instance has the tag, or for a second after an application started. |

| Signal | Arguments | When |
|---|---|---|
| `karotz_status_changed` | `status` i | The rabbit's state changes. |

#### The states

The controller keeps four values. Each value has its own range of numbers. Only the rabbit's state is published (`karotz_status_changed`).

| Value | Number | Name |
|---|---|---|
| network | 2 | NETWORK_ONLINE |
| | 3 | NETWORK_OFFLINE |
| platform | 6 | PLATFORM_ONLINE |
| | 7 | PLATFORM_OFFLINE |
| sleep | 10 | SLEEP_FALSE |
| | 11 | SLEEP_TRUE |
| rabbit | 14 | CONNECTING |
| | 15 | CONNECTED |
| | 16 | AVAILABLE |
| | 20 | UPDATING_CONF |
| | 21 | INTERACTIVE |
| | 22 | INTERACTIVE_STOP |
| | 23 | DISCONNECTED |
| | 24 | SLEEP |
| | 25 | WAKEUP |

What the controller does in each rabbit state:

| State | What the controller does |
|---|---|
| CONNECTED | The platform connection is open. |
| CONNECTING | The controller answered the platform's login. |
| UPDATING_CONF | It plays `acknowledge.mp3` (when sounds are on), installs the configuration (see below), and goes to AVAILABLE. |
| AVAILABLE | If a configuration waits, it goes to UPDATING_CONF. Otherwise it resets: it stops karotzVM, resets the ears, stops the sound, the speech and the recognition, sends `lock::yes` to mpd, ends a call and starts a new karotzVM. When awake, it pulses the nose green (green and blue when messages wait), and moves both ears to position 14. |
| INTERACTIVE | An application has control. The nose pulses light green. |
| INTERACTIVE_STOP | It forgets the application and goes to AVAILABLE. |
| DISCONNECTED | It resets as for AVAILABLE. When awake, the nose pulses red and the ears go to position 4. |
| SLEEP | It looks for an update (see below), moves the ears to 4, stops the sound, speech and recognition, and turns the nose off. |
| WAKEUP | It plays `wakeup.mp3` (when sounds are on and the rabbit was asleep), and goes to AVAILABLE. |

At start, the controller sets NETWORK_ONLINE. Nothing watches the network after that.

#### The platform connection

The platform is a server on the network, for example the PC-side app server (see [app-server.md](app-server.md)). The controller connects to `ip_server`:`port_server` from `karotz.conf` (default port 9123). When `ip_server` is this rabbit (`127.x` or `localhost`, the default when `SERVER=` was not set), it does not connect: its own server is on that port. It logs that no platform server is set.

- Each message is a `PlatformMessage` protobuf (`userland/proto/platform-message.proto`, package `lapinlibre.platform`), with a 4-byte length in network byte order before it.
- A connection has a time limit of 5 seconds. A host name that does not resolve is a failed attempt. Any other connection error stops the controller; immortaldog starts it again.
- Attempts are throttled: the first three go through, then one after 10 seconds, one after 30 seconds, then one each minute. The count goes back to zero when a configuration arrives.
- Login: the platform sends `authenticationToken`. The controller copies `/usr/etc/conf/account.conf` to `/tmp/account.conf` and reads `id` and `pwd` from `[account]`. It answers `authenticationResponse` with the login and the MD5 (hex) of the password followed by the token. If `id` or `pwd` is empty, it does not answer.
- Ping: the first ping is 20 seconds after the connection, then one each 60 seconds. When the platform has sent nothing for 75 seconds (and less than one hour), the ping counts as failed. At the second failed ping, the controller closes the connection and connects again.
- When the connection fails, the controller sets NETWORK_OFFLINE then NETWORK_ONLINE, which connects again. PLATFORM_OFFLINE plays `notify.wav`, forgets the application in control and goes to DISCONNECTED.
- A message that does not parse is not used. The sender gets `response` with the code `ERROR` and a description that names the fields that are missing.

#### The controller's own server

The controller also listens on `port_server` (9123 by default) on all addresses. Applications in karotzVM connect to this server with `karotz.connect("127.0.0.1", 9123)`. The controller reads each connection like the platform's. The commands that an application sends must have its interactive id. A command with another id gets `response` `ERROR`. The messages from the platform that only the platform may send (`authenticationToken`, `config`, `scheduleConfig`, `system`, `contactList`) are used only from the platform's connection.

#### Interactive mode

An application must have control of the rabbit to drive it.

1. The application sends `interactiveMode` with `action` START, an `interactiveId`, and optionally a `configId` and a `ttl`.
2. The controller refuses an empty id (`BAD_INTERACTIVEID`). With a `configId`, the id must be the `uuid` of a configured instance, else `ERROR`.
3. The rabbit must be AVAILABLE. The controller stops the sound, the speech and the recognition, keeps the id and the socket, and goes to INTERACTIVE. It answers `OK` with the interactive id. If the rabbit is not AVAILABLE, it answers `ERROR`.
4. The `ttl` is how long the application may stay silent, 900 seconds at most (also the default). Each 10 seconds the controller checks. When the application was silent for longer than the `ttl`, the controller stops interactive mode and sends `interactiveMode` `action` STOP to the application.
5. `interactiveMode` STOP from the application, `release_control` over D-Bus, or a closed socket also stops interactive mode.

In INTERACTIVE, the daemons' callbacks (button, ears, RFID, ASR, multimedia, events) go to the application's socket, with its interactive id. Otherwise they go to the platform, and only after the login.

#### The protocol messages

The controller sends every message it receives to all its handlers. Each handler uses its own part. The commands need the current interactive id.

| Message field | From | What the controller does |
|---|---|---|
| `authenticationToken` | platform | Answers the login (see above). Goes to CONNECTING. |
| `karotzinfo_query` | any | Answers `karotzinfo_response`: MAC and IP address, flash use of `/usr`, the versions, the language. |
| `config` | platform | Keeps the configuration. Writes or removes `/etc/conf/playsounds` from `playsounds`. Goes to UPDATING_CONF if CONNECTING or AVAILABLE, else installs it later. Answers with the rabbit's details. |
| `scheduleConfig` | platform | Sets the clock from `time` and writes the POSIX TZ for `gmt` (from `/etc/timezone.list`, default Central European) to `/etc/TZ`. Replaces all the schedules. Adds the weekly wake-up and sleep entries. Goes to SLEEP or WAKEUP now, by the nearer of today's times. |
| `system` | platform | `SYSTEM_REBOOT` runs `/sbin/reboot`. `TELNET_ON`, `TELNET_OFF`, `LOG_ON` and `LOG_OFF` are logged and ignored: telnet is set by `TELNET=` in `lapinlibre.txt`, and the system log always runs. |
| `contactList` | platform | Keeps the contact list. |
| `pendingmessage` | platform | Sets the nose: green and blue when messages wait, else green. |
| `interactiveMode` | application | Starts or stops interactive mode. |
| `ping` | application | Answers `OK`. |
| `led` | application | `light`, `fade` or `pulse` to led-daemon. |
| `ears` | application | Moves (`left`, `right`, `relative` for steps) or resets the ears, speed 30. |
| `tts` | application | `START` speaks the text in `lang`. Without `lang`, it uses the rabbit's language: `lang` from `karotz.conf`, else from `account.conf`, else en-US. `STOP` stops. |
| `asr` | application | Starts recognition from a grammar file or a list of words. |
| `multimedia` | application | `play`, `pause`, `stop`, `resume`, `record`, `next`, `previous` and the database lists. Each gets its own `response`. |
| `webcam` | application | `photo` is `video` (a stream of snapshots), `videoStop`, `direct` (one 320x240 JPEG in `webcamDirectResponse`), or a URL for webcam-daemon. |
| `k2k` | application | Call, answer, hang up, or set the account. |
| `serial`, `serialCallback` | application | Open, write, close a port; set the read mode. |
| `choreography` | application | Asks the choreography manager for a script, and runs it in karotzVM. |
| `karotzMessageQuery` | application | Sends it to the platform. |
| `karotzMessageResponse` | platform | Sends it to the application. |
| `getcontactList` | application | Answers with the contact list, or `ERROR` if there is none. |
| `socialQuery` | application | Answers with the configuration's `social` part. |
| `dock` | application | Sends to the dock service (no program on the rabbit has it). |

Most commands get `response` with `OK` or `ERROR`, with the command's `id` as `correlationId`. When the daemon's job ends, the controller sends `event` with the code (`TERMINATED` or `CANCELLED`), with the command's `id` as `correlationId`.

The callbacks that the controller sends:

| Field | When | Contents |
|---|---|---|
| `buttonCallback` | A button gesture | `type`: `SIMPLE`, `DOUBLE`, `MULTIPLE`, `LONG_START`, `LONG_STOP`. |
| `earsCallback` | An ear starts, stops (more than one step) or is blocked | `type`: `START_LEFT`, `START_RIGHT`, `STOP_LEFT`, `STOP_RIGHT`, `BLOCKED_LEFT`, `BLOCKED_RIGHT`; `step`, `length`. |
| `rfidCallback` | A tag comes on the nose or goes | `id`, `content`, `direction` (`IN` or `OUT`), `app`, `type`, `pict`, `color`. |
| `asrCallback` | A recognition result | `recognition`: `text`, `confident` (the score), `semantic` (in `<semantic>`). |
| `multimediaCallback` | The playback changes | The action. |
| `k2kCallback` | A call starts or ends | The other side, the type. |
| `serialCallback` | A line on a serial port | The port, the line. |
| `scheduleEvent` | A schedule that is not the controller's own | `uuid`. |
| `event` | A daemon's job ends | `code`, with the request's id as `correlationId`. Not sent for requests without an id. |

#### The configuration

When the rabbit goes to UPDATING_CONF, the controller installs the configuration (`apps.cc`):

1. The nose pulses orange (when awake).
2. For each `configApp`, it reads `/usr/karotz/apps/<apiKey>/descriptor.xml`. If the version is not the configured version, it downloads `<url_app>/file/<apiKey>/<version>/get` (a zip) and unpacks it with `unzip` into `/usr/karotz/apps/<apiKey>`. An API key may contain only letters, digits, `-`, `_` and `.`, and no `..`.
3. It writes the application's configuration to `/usr/karotz/apps/<apiKey>/config.bin`.
4. It removes the empty folders of applications that are no longer configured.
5. It writes `/tmp/apps.abnf`: the URL `http://<domain_app address>/asr/grammar/<id>.abnf`, the grammar for the long button press.
6. If the configuration has another `lang`, it writes it to `karotz.conf` and loads the new language file.

#### Triggers: tags, the button, speech, schedules

The controller starts a hosted application with one line to karotzVM: `<apiKey> <trigger> <instance>`. For one second after it starts an application, the controller does not start an application from a tag or speech.

| Event | What the controller does |
|---|---|
| Tag on the nose (`tag_in`), when logged in | No light, sound or ear move: the tag listener gives every tag's feedback. It sends `rfidCallback` (in INTERACTIVE, to the application), unless the rabbit is AVAILABLE and a hosted application's instance has the tag. |
| `start_tag_app`, from the tag listener | When AVAILABLE and no application started in the last second: for the instance whose `rfid` is the tag, starts a hosted application with the trigger `RFID;<uid>;<app>;<type>;<pict>;<color>;<content>`; an external one gets the `rfidCallback`. |
| `click`, `dclick` | Sends `buttonCallback` `SIMPLE` or `DOUBLE`. |
| `tclick` | Resets the rabbit. If it was asleep, plays `wakeup.mp3` and wakes up. Goes to AVAILABLE, ears to 14, nose green. |
| `mclick` | With 4 clicks, writes the IP address, the MAC address and the flash use to the log. Sends `buttonCallback` `MULTIPLE`. |
| `lclick_start`, when AVAILABLE | Stops the sound, starts recognition with the grammar file `/tmp/apps.abnf` and fades the nose to orange. Sends `LONG_START`. |
| `lclick_stop` | Sends `LONG_STOP`. |
| `asr_event`, when AVAILABLE | For `<nomatch>` or `<noinput>`, plays `speech_no_match`. For `<error_server_timeout>`, plays `speech_server_timeout_fr-FR.mp3` (French) or `speech_server_timeout_en-GB.mp3`. Else, if the semantic has `<app>`, plays `acknowledge.mp3` and, when the score is above `asr_threshold` (default 30), starts the `<app>` application with the `<par>` instance and the trigger `ASR;<text>;<semantic>` (spaces become `_`). Else plays `speech_not_understood`. Then sends `asrCallback`. |
| `schedule_event` | An application's schedule starts the application (hosted) or sends `scheduleEvent` (other). A one-time schedule is then forgotten. `E_STATUS_KAROTZ_WAKEUP` plays `wakeup.mp3` and stops the controller with status 1; immortaldog starts it again. `E_STATUS_KAROTZ_SLEEP` goes to SLEEP. |
| `incoming_call` | Gives the call to the k2k application in karotzVM. |

The controller does the `tclick` reset and the `mclick` log line also before the platform login. It does the other button actions, and sends the button callbacks, only after the login.

#### The update check

When the rabbit goes to SLEEP, the controller downloads `<url_update>/rootfs_version` and `<url_update>/yaffs_version`, and compares their version stamps (`YY.MM.DD.HH`, on the fourth line) with `/etc/conf/rootfs_version` and `/etc/conf/sys_version`. If a newer one exists, the rabbit restarts. The install sets `url_update` to `http://127.0.0.1:1`, a closed port, so the check fails at once.

#### karotzVM and the controller

The controller starts `/usr/karotz/bin/karotzVM --app_folder=/usr/karotz/apps`, with a pipe on its standard input and `/dev/null` on its standard output. Each reset stops karotzVM (`killall karotzVM`) and starts a new one. The controller writes one line per application to the pipe, at most 4094 characters. The fields cannot contain spaces or control characters: the controller refuses an unsafe API key or instance, and changes such characters in the trigger to `_`. If karotzVM stopped outside a reset, the controller starts a new one for the next line.

### karotzVM

karotzVM (`/usr/karotz/bin/karotzVM`) runs the applications' JavaScript with V8 (2.5.9.23). The sources are in `userland/src/usr/karotzVM`.

#### How karotzVM runs an application

1. karotzVM reads one line from its standard input: `<apiKey> [<launchType> [<instance>]]`. The line is split at the first two spaces. An empty line or the end of the input stops the reading; the applications that run continue.
2. It starts each application in a child process (`fork`). At most 3 applications run at the same time. A fourth line is dropped and logged (`too many kvm ignore request`). When `fork` fails, the place is given back at once.
3. The child reads `<app_folder>/<apiKey>/descriptor.xml`. Only `/descriptor/accesses/access` is used: each `<access>` gives the application one API object. If the descriptor cannot be read, the child stops.
4. The child reads `<app_folder>/<apiKey>/config.bin` (the instance settings from the configuration) into the global `params`.
5. The child runs `<app_folder>/<apiKey>/main.js` once, then runs a GLib main loop.
6. The application connects to the controller (`karotz.connect`) and asks for control (`karotz.start`). It must have control within 10 seconds after `main.js` has run, or the child stops (`AUTOKILL`).
7. The application ends with `exit()`. It also ends when the controller refuses control (exit status 1), when the connection closes, or 5 minutes after the controller stops interactive mode.

Options: `--app_folder=<dir>` (the controller gives `/usr/karotz/apps`) and `--debug=<n>` (also writes the messages up to that GLib level to standard error). The V8 heap limits are 2 MB (young) and 10 MB (old). karotzVM logs to syslog as `KarotzVM` (facility local1). JavaScript `log(text)` writes `JSLOG: text`.

karotzVM does not call D-Bus. Each API call is a `PlatformMessage` that it sends to the controller over the TCP connection, with a new UUID as `id` and the application's `interactiveId`. The controller sends the commands to the daemons. karotzVM matches each reply to its request by `correlationId`. A reply that nobody waits for is logged as `NO CORRELATION FOUND`.

#### Callbacks

Most functions take an optional callback. A "code callback" gets the name of a `ReturnCode`: `OK`, `ERROR`, `TIMEOUT`, `TERMINATED`, `CANCELLED`, `BUSY`, `BAD_INTERACTIVEID`, `SCHEDULE_EVENT`, `NOT_CONNECTED`. It is called once for the controller's `response` (usually `OK` or `ERROR`) and again for the `event` that ends the job (usually `TERMINATED` or `CANCELLED`). Thus one command can give `OK`, then `TERMINATED`.

A listener (`addListener`) gets each matching callback message. It stays registered only while it returns `true`. The serial listener is different: it stays registered unless it returns `false`. A listener that throws an exception stays registered, and the exception is logged.

Wrong arguments throw `"Wrong arguments"`. The functions marked "control" need interactive mode, and throw `"Must be in interactive mode"` without it.

#### The launch values

| Global | What it holds |
|---|---|
| `appName` | The API key. |
| `instanceName` | The instance name. |
| `cmdline` | The whole line that karotzVM read. |
| `params` | One property per instance name, with the instance's `{key: value}` pairs (and `accessToken` and `uuid` when set); `params.descriptor` holds the application's parameters. `{}` when there is no `config.bin`. |
| `launchType` | How the application was started (see below). |

| Trigger | `launchType` |
|---|---|
| RFID tag | `{name: "RFID", id, app, type, pict, color, data}`, all strings. `data` is the tag content. |
| Speech | `{name: "ASR", text, rawSemantic, semantic, confident: 50}`. `semantic` is the `<semantic>` XML as an object. Spaces in the text are `_`. |
| Schedule | `{name: "SCHEDULER"}` |
| Incoming k2k call | `{name: "%recv%"}` |
| Other | `{name: <the second field>}` |

#### The JavaScript API

The global functions and objects:

| Name | Arguments | What it does |
|---|---|---|
| `log(text)` | 1 | Writes `JSLOG: text` to syslog. |
| `exit()` | 0 | Stops the application. |
| `setTimeout(ms, fn)` | number, function | Calls `fn` after `ms`. It calls it again while `fn` returns `true`. There is no id and no `clearTimeout`. |
| `include(name)` | string | Runs another file of the application. The name may contain only `A-Z a-z 0-9 _ / - .`, no `..`, and not `descriptor.xml`. |
| `ping([cb])` | control | Sends `ping`; code callback. |
| `uuid()` | | A new lower-case UUID. |
| `timestamp()` | | The time in seconds, as a string. |
| `signHMACSHA1(text, key)` | 2 strings | base64 of HMAC-SHA1. |
| `file.read(name)` | string | An object whose `text` property reads `<app_folder>/<apiKey>/<name>`. The object can go to `karotz.multimedia.play` or `http.post`. |
| `file.write(name, text)` | 2 strings | Writes the file in the application's folder. |
| `http.get(url [, headers])` | access `http` | The body, as a string. Blocks until done. |
| `http.get2(url [, headers])` | access `http` | `{header, content}`. |
| `http.post(url, params [, headers [, isForm]])` | access `http` | The body. `params` is URL-encoded, or a multipart form when `isForm` is true. |
| `http.post2(...)` | access `http` | `{header, content}`. |
| `social.init(cb [, data])` | | Gets the `social` part of the configuration, adds `social.twitter`, `social.facebook` and `social.soundcloud` for the granted accesses, then calls `cb(data)`. |

`http` must connect within 10 s, and a transfer slower than 1 byte a second for 30 s fails. On a curl error the content is `"curl error"`.

`http` checks HTTPS servers' certificates, and the `social` posts use the same checks. The certificate must lead to one of the CA certificates in `/etc/ssl/certs` (from the ca-certificates package, one file per certificate, named by its subject hash; `$SSL_CERT_DIR` replaces the directory), and it must name the host. A request that fails the check gets `"curl error"`, and the log has a `certificate check failed` warning that gives the reason. OpenSSL reads only the CA certificate that a server's chain needs. On the rabbit, the check adds about 0.2 s of CPU to a request; curl's single bundle file, `ca-certificates.crt`, would add about 1.1 s. The rabbit's clock reads 1970 at every boot until `ntpd` sets it. While the clock is earlier than 2026, an HTTPS request waits for it to be set, for up to 60 s, so the dates are always checked against the real time. If the clock is still not set after 60 s, the request goes ahead and fails the check, and later requests from the same karotzVM no longer wait. There are no exceptions: the checks can't be turned off, and a server with a self-signed, expired or wrong-name certificate is refused.

The `karotz` object:

| Function | Arguments | What it does |
|---|---|---|
| `karotz.connect(host, port)` | string, number | Opens the TCP connection to the controller. Throws `"connection refused"`. |
| `karotz.start(cb [, data])` | function, object | Sends `interactiveMode` START with a new interactive id. On `OK` it calls `cb(data)`. On another code the application stops (status 1). Throws `"Connect first"` without a connection. |
| `karotz.stop()` | control | Sends `interactiveMode` STOP. |
| `karotz.ping([cb])` | control | As `ping`. |
| `karotz.info(cb)` | control | Sends `karotzinfo_query`. `cb` gets an object with the rabbit's details. |
| `karotz.random()` | | A number from 0 to 9999999. |

The objects that an `<access>` gives (`karotz.<access>`):

| Function | Arguments | Callback and events |
|---|---|---|
| `karotz.led.light(color [, cb])` | control; `RRGGBB` | Code callback. |
| `karotz.led.fade(color, period [, cb])` | control; ms | Code callback. |
| `karotz.led.pulse(color, period, pulse [, cb])` | control; ms, ms | Code callback. |
| `karotz.ears.move(left, right [, cb])` | control; positions, `null` leaves an ear | Code callback. A move of both ears that is blocked can give `CANCELLED` twice. |
| `karotz.ears.moveRelative(left, right [, cb])` | control; steps | Code callback. |
| `karotz.ears.reset([cb])`, `resetLeft([cb])`, `resetRight([cb])` | control | `cb(code, left, right)`. |
| `karotz.ears.addListener(fn)` | | `fn(type, step, length)`; `type` is `START_LEFT`, `START_RIGHT`, `STOP_LEFT`, `STOP_RIGHT`, `BLOCKED_LEFT`, `BLOCKED_RIGHT`. |
| `karotz.tts.start(text [, lang] [, cb])` | control | Code callback: `OK`, then `TERMINATED` or `CANCELLED`. Without `lang`, the controller uses the rabbit's language: `lang` from `karotz.conf`, else from `account.conf`, else en-US. |
| `karotz.tts.stop([cb])` | control | Code callback. |
| `karotz.multimedia.play(url [, cb])` | control; a path, a URL, an mpd command, or the `data` of a recording or photo; `./name` is a file of the application | Code callback. |
| `karotz.multimedia.pause`, `resume`, `stop`, `next`, `previous` `([cb])` | control | Code callback. |
| `karotz.multimedia.record([cb])` | control | `cb({type})` for the response, then `cb({type, description, data})` at the end; `data` is the recording (`/tmp/record.flac`). |
| `karotz.multimedia.artist`, `folder`, `genre`, `playlist`, `song` `(cb)` | control | `cb(list)`, a string `a:b:c:`. |
| `karotz.multimedia.addListener(fn)` | | `fn(action)`: `PLAY`, `STOP`, `PAUSE`, `RESUME`, `MUSIC_UPDATE`. |
| `karotz.asr.string(grammar, lang, cb)` | control | `cb(result)`: `text`, `confident` and the properties of the semantic XML. |
| `karotz.asr.file(name [, cb])` | control; a grammar file of the application | As `string`. |
| `karotz.button.addListener(fn)` | | `fn(type)`: `SIMPLE`, `DOUBLE`, `TRIPLE`, `MULTIPLE`, `LONG_START`, `LONG_STOP`. |
| `karotz.rfid.addListener(fn)` | | `fn(tag)`: `id`, and when present `direction` (`IN`, `OUT`), `app`, `type` (`NONE_TYPE`, `FLAT`, `NANOZTAG`, `ZTAMPS`, `DRESS`, `BOOKZ`), `pict`, `color` (`NONE_COL`, `RED`, `BLUE`, `GREEN`, ...), `content`. |
| `karotz.webcam.photo(url [, cb])` | control; `direct`, `video`, `videoStop`, `file` or an upload URL | `cb({type})`, then `cb({type, description, data})`; `data` is `/tmp/picture.jpg`. For `direct`, one `cb({type, data})`: the JPEG from the controller, written to `/tmp/picture.jpg`. |
| `karotz.serial.open(port [, baud])` | control; default 9600 | None. |
| `karotz.serial.write(data [, base])` | control; to the last opened port; `base` `a` (text, default) or hex | None. |
| `karotz.serial.addListener(fn [, base])` | control | `fn(line)` for each line of the last opened port. |
| `karotz.k2k.call(address)`, `answer(accept)`, `hangup()`, `account(sipId, sipDomain, sipPassword, stunServer)` | control | None. |
| `karotz.k2k.addListener(fn)` | | `fn({type, correspondant})`: `INCOMING`, `BEGINNING`, `TERMINATED`. |
| `karotz.contact.get(cb)` | control | `cb(array)` of `{firstname, lastname, alias, uuid, objects}`; `[]` when there is no list. |
| `karotz.message.list(cb)` | control | `cb(array)` of `{header: {id, date, status, from, to}, content: {type, content}}`. |
| `karotz.message.get(ids, cb)`, `delete(ids, cb)`, `markAsRead(ids, cb)` | control | As `list`. |
| `karotz.message.new(to, text [, cb])` | control; `to` is `{uuid, objects}` | Sends a text message. Code callback, once: `OK` or `ERROR`. |
| `karotz.dock.light`, `fade`, `pulse`, `move(direction [, time [, cb]])` | control | Code callback. No program on the rabbit gives the dock service. |
| `karotz.dock.addListener(fn)` | | `fn(status, change)` for each dock status message from the controller; `fn` stays registered while it returns `true`. |

Accesses with no `karotz` object: `http` (gives the global `http`), and `twitter`, `facebook`, `soundcloud` (used by `social.init`).

### karotz-player

karotz-player (`/usr/bin/karotz-player`) plays sound files, URLs and web radio. multimedia-daemon, the button watcher and `startup.sh` use it.

```
karotz-player [-include CONF] [-playlist] [-loop N] [-idle] [-input file=FIFO] PATH_OR_URL
```

| Option | What it does |
|---|---|
| `-include CONF` | Reads `key=value` lines from CONF. |
| `-playlist` | The item is a playlist (`.m3u`, `.pls` or `.asx`, a file or a URL). |
| `-loop N` | Plays the list N times. |
| `-idle` | Keeps the player running after the last item. |
| `-input file=FIFO` | Reads commands from FIFO. |
| `-afm`, `-ao`, `-ac`, `-af`, `-volume`, `-cache`, `-user-agent`, `-softvol-max` | Accepted with their value, and not used. |
| other options (`-slave`, `-quiet`, ...) | Accepted and not used. |

The keys in the CONF file:

| Key | What it does |
|---|---|
| `input=file=FIFO` | The command FIFO. |
| `idle=yes` | As `-idle`. |
| `ao=oss:DEVICE` | The output device (default `/dev/dsp`). |
| `loop=N` | As `-loop`. |

`/etc/conf/karotz-player.conf` has `input=file=/tmp/karotz-player.ctrl` and `idle=no`. Thus the player stops after the last item, and multimedia-daemon starts a new player for the next play.

The commands, one per line on the FIFO:

| Command | What it does |
|---|---|
| `loadfile ARG [0\|1]` | Plays ARG. 0 replaces what plays, 1 adds it to the end. ARG can be in single or double quotes. |
| `loadlist ARG [0\|1]` | The same, for a playlist. |
| `pause` | Pauses, or continues after a pause. |
| `stop` | Stops and empties the list. |
| `quit [CODE]` | Stops the player with the exit code CODE. |

The player makes the FIFO if it does not exist, and opens it for reading and writing, so a writer that closes it does not end the reading. Audio is decoded with FFmpeg (Vorbis with Tremor, in fixed point) and written to the OSS device as 16-bit samples, at the stream's rate and channels. The processor has no floating-point unit, so nothing is converted when the samples already match the device (the rabbit's own sounds are 22 kHz mono). During a pause, a network stream is still read and dropped, so the server keeps the connection. SIGTERM stops the player at once.

### The settings files

The settings are in `/usr/etc/conf` (also `/etc/conf`). The install writes most of them (see [install.md](install.md)).

| File | Contents | Read by |
|---|---|---|
| `karotz.conf` | `[karotz]`: `ip_server`, `port_server`, `url_update`, `url_app`, `domain_app`, `lang`, `asr_threshold`. `[k2k]`: `sipDomain`, `stunServer`. | controller (which also writes `lang`), k2k-daemon, the web API |
| `account.conf` | `[account]`: `id`, `pwd`, the platform login. | controller (through `/tmp/account.conf`), k2k-daemon |
| `voice.conf` | `[tts]`: one URL per language (`en`, `fr`, `de`, `es`, `it`, `nl`, `pt`). `[asr]`: `nbserver`, `server0`..., `port`. | voice-daemon, the web API |
| `karotz-player.conf` | `input=file=/tmp/karotz-player.ctrl`, `idle=no`. | karotz-player (from multimedia-daemon) |
| `playsounds` | A flag file: when it exists, the button tones and the controller's sounds play. | controller, dbus_watcher; written by `S40lapinlibre` (first boot) and the controller (from the platform's `playsounds`) |
| `rfid_conf.xml` | `writeInTag` and the `block7` codes. | rfid-daemon |
| `linphone.conf` | The SIP and audio settings of liblinphone. | k2k-daemon |
| `wpa.conf` | The Wi-Fi network block for `wpa_supplicant`. | `network.sh` |
| `network.conf` | `IP`, `NETMASK`, `GATEWAY`, `DNS`. No `IP` (or no file) means DHCP. | `network.sh` |
| `hostname` | The rabbit's name. | `linuxrc` |
| `telnet` | When it exists, `inetd` also gives telnet. | `inetd.sh` |
| `web_password`, `web_password.hash` | The web password. | `inetd.sh` |
| `dropbear/` | The SSH host keys. | Dropbear |
| `rootfs_version`, `sys_version`, `rootfs_status` | The installed versions. | controller (`karotzinfo_response`, update check) |
| `lapinlibre_build_id` | The installed build. | the install, the web API |

Other files that the system reads at run time:

| File | Contents | Read by |
|---|---|---|
| `/usr/karotz/uuid.conf` | The rabbit's UUID, made once. | the install, the web API |
| `/usr/karotz/res/lang/<lang>` | `[lang]`: the sentences and sounds of a language (`de-DE`, `en-GB`, `en-US`, `es-ES`, `fr-FR`). | controller |
| `/etc/timezone.list` | `<zone>;<POSIX TZ>` lines. | controller |
| `/etc/TZ` | The POSIX time zone. uClibc reads it, so a change reaches all programs. | all programs; written by `startup.sh` and the controller |
| `/etc/mpd/mpd.conf` | mpd's settings. | mpd |
| `/usr/karotz/.ssh/authorized_keys` | The SSH public key. | Dropbear |

### The logs

All the programs log to syslog. `linuxrc` starts BusyBox `syslogd` with no options, so the log is `/var/log/messages`, which turns over to `/var/log/messages.0`. `/var` is in RAM, so the log is lost at each restart.

- The daemons log with their own names (see [The daemons](#the-daemons)), facility local1, notice and up. The controller, ears-daemon, voice-daemon and webcam-daemon also log debug messages when `KAROTZ_DEBUG` is set.
- immortaldog logs as `ImmortalDog`. The scripts log with `logger`: `lapinlibre` (`linuxrc`), `startup`, `network`, `usbkey`, `inetd.sh`, `S40lapinlibre`, `dbus_watcher`, `tag_listener`.
- mpd logs to `/var/log/mpd.log`. k2k-daemon with `--debug` writes `/tmp/linphone.log`.
- `dmesg` shows the kernel messages.

The web API's `lapinlibre/logs` endpoint shows the system log, the kernel messages and the mpd log (see [web-api.md](web-api.md)).
