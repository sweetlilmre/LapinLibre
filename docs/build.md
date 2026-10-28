<!-- SPDX-License-Identifier: MIT -->
# Build and test

This page tells you how to build LapinLibre from source, how to make the USB key, and how to run the tests. The first part gives the procedures. The second part tells how the build and the tests work. The install of the USB key on the rabbit is in [install.md](install.md). To install LapinLibre, you don't need to build it: each release on the [releases page](https://github.com/sweetlilmre/LapinLibre/releases) has the USB key as a zip.

## How to

Do these procedures in a Linux shell, in the root of the repository. On Windows, use a WSL 2 shell.

### Prepare the build host

Use this procedure one time on a new build host.

1. Use Ubuntu 24.04 or a different Debian-like Linux, natively or in WSL 2.
2. Make sure that Python 3.11 or later is installed: `python3 --version`.
3. Find the missing packages: `bash scripts/host-prereqs.sh`.
4. Install the missing packages: `bash scripts/host-prereqs.sh --install`. This command uses `sudo apt-get`.
5. On Ubuntu 24.04, let unprivileged users make user namespaces: `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`. The kernel stage and the tests need user namespaces.
6. Do the check stage: `./build.sh check`. The stage must show `host prerequisites: all present` and `0 problems`.

### Build the USB key

Use this procedure to build all the stages and make the USB key.

1. Run `./build.sh`. The command does all the stages in sequence: `check fetch toolchain system v8 kernel userland image test`.
2. Wait. The first build takes a long time, because Buildroot builds all the rabbit's packages, and the toolchain too if `config.toml` pins no published toolchain. The next builds skip the stages that are up to date.
3. Read the last lines of the `image` stage. They give the build id, the size of each image and the folder of the key.
4. Find the work location: `./build.sh where`.
5. Find the key in `out/usbkey` below the work location.
6. Copy the contents of `out/usbkey` (not the folder) to the root of an empty FAT32 USB key. Then follow [install.md](install.md).

### Build one stage

Use this procedure to do one stage or some stages only.

1. Run `./build.sh STAGE`, for example `./build.sh userland`.
2. To do more than one stage, give each stage in sequence: `./build.sh userland image`.
3. Make sure that the stages before it are done. For example, `userland` needs `system` and `v8`. The table [Stages](#stages) gives each stage's inputs.

### Set the work location

Use this procedure to write the build to a different folder.

1. Make a folder on a Linux file system, for example `mkdir -p ~/lapinlibre-work`.
2. Set the variable for each build command: `LAPINLIBRE_WORK=~/lapinlibre-work ./build.sh`.
3. Do a check of the location: `LAPINLIBRE_WORK=~/lapinlibre-work ./build.sh where`.

Do not set the work location on a Windows drive (`/mnt/...`). It is slow and it does not keep the case of file names.

### Remove the build results

Use this procedure to start a build again from the beginning.

1. To remove `work/` and `out/` and keep the downloads, run `./build.sh clean`.
2. To remove `work/`, `out/` and `dl/`, run `./build.sh clean-all`. The next build downloads all the files again.

### Check a USB key's own files

Use this procedure after the `image` stage, before you copy the key.

1. Run `bash system/check-image.sh`. It tests the key in `out/usbkey`.
2. To test a key in a different folder, give the folder: `bash system/check-image.sh /path/to/key`.
3. Read the result. Each check shows `PASS` or `FAIL`. The exit status is not zero if a check failed.
4. To test the boot files of the key, run `bash system/tests/core-boot.sh`.

### Run all the tests

Use this procedure to run all the test suites that `tests/run.sh` runs.

1. Make sure that the stages `system`, `v8`, `kernel` and `userland` are done.
2. Run `./build.sh test`. You can also run `bash tests/run.sh`.
3. Read the summary at the end: the number of tests that passed, failed and were skipped.
4. Look at the full log in `work/tests/run.log` below the work location.

### Run one behaviour scenario

Use this procedure to run one scenario of `tests/scenarios`.

1. Run `bash tests/run.sh NAME`, for example `bash tests/run.sh led_daemon`. The command makes the test sysroot and the test preloads, then runs only that scenario.
2. To run more than one scenario, give each name: `bash tests/run.sh led_daemon ears_daemon`.
3. If the scenario fails, read the diff in the output. The working folder of the run stays, with its logs and `record.txt`.

To run a scenario with the harness directly (after step 1 one time):

```
W="$(./build.sh where)/work"
python3 -I -B tests/harness.py --sysroot "$W/sysroot-core" --build "$W/userland/system" \
    --fakes "$W/tests" led_daemon
```

### Run one aggressive test

Use this procedure to run one test of `tests/aggressive`.

1. Run one scenario one time (for example `bash tests/run.sh led_daemon`). This makes the test sysroot and the test preloads.
2. Set the work folder: `W="$(./build.sh where)/work"`.
3. Run the test with the harness and `--suite aggressive`:

```
python3 -I -B tests/harness.py --suite aggressive --sysroot "$W/sysroot-core" \
    --build "$W/userland/system" --fakes "$W/tests" led_long_id
```

4. Read the result. `PASS` shows that all the checks hold. `FAIL` shows each problem and the folder that keeps the logs and `record.txt`.

### Record the expected result of a scenario again

Use this procedure only when the behaviour of a program changed on purpose.

1. Record one scenario: `bash tests/run.sh --record NAME`. To record all scenarios, run `bash tests/run.sh --record`.
2. Look at the changes: `git diff tests/expected/`.
3. Make sure that each change agrees with the change of behaviour.
4. Commit the changed files in `tests/expected/` with the change of the program.

### Run one image test or the app server tests

Use this procedure to run one Python test file alone.

1. Run all the image tests: `python3 -I -B tests/image/run.py`.
2. Run one image test file: `python3 -I -B tests/image/test_fstree.py`. Use the same command for `test_usbkey.py`, `test_core_boot.py`, `test_tzlist.py` and `test_web_api.py`.
3. Run the app server tests with the system build's `protoc`: `python3 -I tests/app_server/test_app_server.py "$(./build.sh where)/work/system/out/host/bin/protoc-2.3.0"`.

### Add a behaviour scenario

Use this procedure to add a test that compares a program's behaviour with a stored record.

1. Make a module `tests/scenarios/NAME.py`.
2. In the module, set `DAEMON` (the program name in the userland build, for example `led-daemon`).
3. Set `DEVICES`, the list of device names that the program opens below `/dev` (for example `["spidev0.1"]`).
4. Write `drive(daemon, bus)`. It drives the program and returns the first lines of the record (a list of strings).
5. If necessary, add the optional hooks. The table [Scenario module](#scenario-module) gives them.
6. Add `NAME` to `SCENARIOS` in `tests/run.sh`. Also add it to `SCENARIOS` in `system/check-image.sh` if the key's own sysroot must run it.
7. Record the scenario: `bash tests/run.sh --record NAME`.
8. Read the new file `tests/expected/NAME.txt` line by line. Make sure that it shows the correct behaviour.
9. Run the scenario again: `bash tests/run.sh NAME`. It must show `PASS`.

### Add an aggressive test

Use this procedure to add a test that sends hostile or broken input and checks the result with code.

1. Make a module `tests/aggressive/NAME.py`. The name must start with a lowercase letter. A module whose name starts with `_` is a helper, and `tests/run.sh` does not run it.
2. Set `DAEMON` and `DEVICES`, and write `drive(daemon, bus)`, as for a scenario. You can import the parts of a scenario, for example `from scenarios import led_daemon as led`.
3. Write `check(record)`. It returns a list of problems. An empty list is a pass.
4. If you want a measurement in each result, write `summary(record)`. It returns one line of text.
5. Run the test (see [Run one aggressive test](#run-one-aggressive-test)). `tests/run.sh` runs every module in `tests/aggressive` automatically.

### Run the device tests on a rabbit

Use this procedure to test on a real rabbit. These tests run from RAM and write nothing to the flash. Start a test only when the rabbit is free for it: some tests move it, make it speak or restart it.

1. To test `immortaldog`, copy a build to RAM on the rabbit:

```
ssh karotz@<rabbit> 'mkdir -p /dev/shm/new && cat > /dev/shm/new/immortaldog && chmod 755 /dev/shm/new/immortaldog' \
    < <build>/usr/immortaldog
```

2. Run the test script on the rabbit:

```
ssh karotz@<rabbit> sh -s -- /dev/shm/new/immortaldog /dev/shm/immtest 50 < tests/device/immortaldog.sh
```

3. To test the web page and the API, copy the new files to RAM on the rabbit:

```
tar -C image/core/usr -cf - www share/lapinlibre scripts/tag_listener | \
    ssh root@<rabbit> 'rm -rf /dev/shm/web && mkdir -p /dev/shm/web && tar -C /dev/shm/web -xf -'
```

4. Run the test script on the rabbit, with a test password of your choice:

```
ssh root@<rabbit> sh -s -- /dev/shm/web/www /dev/shm/web/share/lapinlibre/web.sh /dev/shm/webtest <test password> \
    < tests/device/web-api.sh
```

5. To also move the LED, the ears, the sound and the camera, add `hardware` after the password. Add `tag` to test an RFID tag, and `reboot` to test the reboot endpoint. Use these options only with someone at the rabbit.
6. Read the result. Each check shows `PASS` or `FAIL`.

### Make a release

Use this procedure to publish a USB key on the repository's releases page. The `release` workflow (`.github/workflows/release.yml`) builds the key from the tag. It does not run the tests, so you run them first.

1. Commit all the changes. `git status` must show no changed files.
2. Run `./build.sh`. All the stages and the tests must pass.
3. Choose the version: the last release (`git describe --tags --match 'v[0-9]*' --abbrev=0`) with the minor number or the patch number increased.
4. Make the tag: `git tag v0.1.0`.
5. Push the branch and the tag: `git push origin main v0.1.0`.
6. On GitHub, open Actions, then the `release` run of the tag. The run takes about 40 minutes with the published toolchain (v0.1.0: 36.5 minutes).
7. When the run is done, open Releases. The release `v0.1.0` has `LapinLibre-v0.1.0.zip` and `LapinLibre-v0.1.0.zip.sha256`. The notes give the build id.

To build the zip without a release, open Actions, then `release`, then "Run workflow", and choose a branch. The zip is kept with the run for 7 days.

If the run fails, delete the tag (`git push origin :refs/tags/v0.1.0` and `git tag -d v0.1.0`), correct the problem, and start again from step 1.

### Publish a new toolchain

Use this procedure when the toolchain changes: a new GCC, binutils, uClibc-ng or Buildroot version, a change to `system/toolchain/buildroot.config`, or to the uClibc-ng patches or options.

1. In `config.toml`, increase the last number of `toolchain.name`, for example `toolchain-gcc14.3.0-uclibc-ng1.0.57-2`. Change the versions in the name if they changed.
2. Remove the `[download.toolchain]` table from `config.toml`. Then the build builds the toolchain.
3. Build and test LapinLibre with the new toolchain: run `./build.sh`. All the stages and the tests must pass.
4. Commit and push the changes.
5. On GitHub, open Actions, then `toolchain`, then "Run workflow" on `main`. The run builds the toolchain and makes the release `NAME` with `NAME.tar.gz` and `NAME.tar.gz.sha256`. It stops if that release already exists.
6. Add `[download.toolchain]` to `config.toml` with `group = "toolchain"`, the release file's URL in `urls`, `file = "NAME.tar.gz"`, and the SHA-256 from `NAME.tar.gz.sha256`.
7. Run `./build.sh toolchain system`. The stage fetches the published toolchain. Then commit.

### Change a pinned download

Use this procedure to add a download or change its version.

1. Open `config.toml`.
2. Add or change a table `[download.NAME]`. Give `group`, `urls`, `sha256` and, if necessary, `file`. For a git download, give `git` and `commit`.
3. Get the file and calculate its SHA-256 on your computer. Write the value in `sha256`.
4. Run `python3 -I -B scripts/fetch.py NAME`. The command downloads the file and prints its path. It stops with an error if the checksum is not correct.

## Technical details

This part tells how the build and the tests work, for the reader who wants to change them.

### Build host

This section gives what the build host must have.

| Item | Requirement | Where the build checks it |
|---|---|---|
| Distribution | Ubuntu 24.04 or a different Debian-like Linux, natively or in WSL 2. The package check uses `dpkg-query` and the install uses `apt-get`. | `scripts/host-prereqs.sh` |
| Packages | General build tools: `build-essential bison flex gawk texinfo patch perl bc cpio rsync file unzip bzip2 xz-utils wget ca-certificates git python3 gettext libncurses-dev zlib1g-dev`. The key's sounds: `ffmpeg`. The kernel namespace: `util-linux`. The code generators and the host build of the daemons: `libglib2.0-dev libdbus-glib-1-dev libdbus-1-dev protobuf-compiler libprotobuf-dev libcurl4-openssl-dev libjpeg-dev libexpat1-dev uuid-dev libxml2-dev`. The tests: `qemu-user dbus gcc-arm-linux-gnueabi`. The command `pkg-config` (from `pkg-config` or `pkgconf`). | `scripts/host-prereqs.sh` |
| Python | 3.11 or later. The build tools use `tomllib` and the standard library only. Every call is `python3 -I -B`, so Python writes no bytecode into the repository. `LL_PY` sets a different interpreter. | `scripts/host-prereqs.sh`, `scripts/common.sh` |
| User namespaces | `unshare --user --map-root-user true` must work. On Ubuntu 24.04, set `kernel.apparmor_restrict_unprivileged_userns=0`. | `scripts/host-prereqs.sh` |

Notes for WSL:

- When the repository is on a Windows drive (`/mnt/...`), the build writes below `~/.cache/lapinlibre` in the Linux file system. A Windows drive is slow and does not keep the case of file names.
- WSL adds the Windows `PATH`, which has spaces. Buildroot refuses such a `PATH`, so the system stage runs Buildroot with a clean environment and `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.
- The test sysroots must be on a Linux file system, because they need symbolic links and file modes.

### Work location

This section tells where the build writes and how it keeps results between builds.

`scripts/common.sh` sets the work location (`LL_LOCATION`) in this sequence:

1. `LAPINLIBRE_WORK`, if it is set.
2. `$HOME/.cache/lapinlibre`, if the repository is below `/mnt/`.
3. The repository itself, in all other cases. `.gitignore` excludes `/dl/`, `/work/` and `/out/`.

| Folder | Variable | Contents |
|---|---|---|
| `dl/` | `LL_DL` | The download cache. `dl/br2026/` holds Buildroot's downloads. `dl/git/NAME.git` holds a bare mirror of each git download. |
| `work/` | `LL_WORK` | The build trees, the stamps (`work/stamps/`), the host tools (`work/hosttools/`), the test preloads and log (`work/tests/`), and the sysroots (`work/sysroot-core`, `work/sysroot-image`). |
| `out/` | `LL_OUT` | The results: `out/usbkey/`, `out/system-kernel/` and `out/install-tools/`. |

Caching:

- A stamp is a file in `work/stamps/` that holds a key. The key is a hash of the contents of all the inputs of a step (`ll_hash`: file contents and names, sorted). A step with a stamp that holds the same key is skipped.
- The `system`, `v8` and `kernel` stages use stamps. The `userland`, `install-tools` and `image` steps run each time.
- An interrupted Buildroot build continues from where it stopped, if the inputs of Buildroot did not change (see [system](#system)).
- The kernel's host `gzip` 1.3.12 is built one time, in `work/hosttools/gzip-1.3.12`.

Other variables:

| Variable | Effect |
|---|---|
| `LAPINLIBRE_WORK` | Sets the work location. |
| `LAPIN_JOBS` | The number of parallel jobs for `v8` and `userland` (default: `nproc`). |
| `LAPIN_TOOLCHAIN` | `build` builds the toolchain from source even when `config.toml` pins a published one. `download` uses the pin. |
| `V8_CROSS` | A different toolchain prefix for `v8`. Then the stamp of `system` is not an input of `v8`. |
| `SOURCE_DATE_EPOCH` | The time stamp of the files that the image makes, and of `install-tools.tar`. Without it, `image.py` uses the time of the last commit, and `install-tools.sh` uses 0. |
| `LL_PY` | The Python interpreter (default: `python3`). |

### Commands of build.sh

This section gives the commands of `./build.sh`.

`./build.sh [STAGE...]` does each stage in the sequence given. Without a stage, it does all the stages. Each stage writes `== stage: NAME` and its time when it is done. Messages go to standard error. A stage that fails stops the build (`set -euo pipefail`).

| Command | What it does |
|---|---|
| `check` | Runs `scripts/host-prereqs.sh` and `scripts/check-repo.py`. |
| `fetch` | Gets every download in `config.toml` into `dl/` (`scripts/fetch.py --all`). |
| `toolchain` | Gets the cross toolchain: the published one, or builds it with Buildroot (`system/toolchain/build.sh`). |
| `system` | Builds the rabbit's packages with Buildroot and that toolchain (`system/build.sh`). |
| `v8` | Builds `libv8.so` and `v8-shell` (`system/v8/build.sh`). |
| `kernel` | Builds the kernel (`system/kernel/build.sh`). |
| `userland` | Builds LapinLibre's programs (`system/userland.sh`), then the key's install tools (`system/install-tools.sh`). |
| `image` | Makes the USB key (`image/build.sh`, which runs `image/tools/image.py`). |
| `test` | Runs the tests (`tests/run.sh`). |
| `clean` | Removes `work/` and `out/`. |
| `clean-all` | Removes `work/`, `out/` and `dl/`. |
| `where` | Prints the work location. |

### Stages

This section gives the inputs and the outputs of each stage. The sections after it tell about each stage.

| Stage | Inputs | Outputs |
|---|---|---|
| `check` | The host, the files of the repository (`git ls-files`) | A report only |
| `fetch` | `config.toml` `[download.*]` | `dl/` |
| `toolchain` | `config.toml` `[toolchain]`, and the `toolchain` download; or to build it, `system/toolchain/`, `system/patches/uclibc`, `system/external/uclibc-ng.fragment` and the `system-source` and `system` downloads | `work/toolchain/sdk/`; when built, `out/toolchain/NAME.tar.gz` |
| `system` | `system/buildroot.config`, `system/patches/`, `system/external/`, `config.toml`, the `system-source` and `system` downloads | `work/system/out/` (`host/` toolchain, `target/`, `staging/`, `build/`, `build.log`) |
| `v8` | `system/v8/`, the `v8-branch-src` download, the system toolchain | `work/v8/libv8.so`, `work/v8/v8-shell`, `work/v8/src/` (headers) |
| `kernel` | `kernel/src/`, `kernel/yaffs.py`, `system/kernel/`, `config.toml` `[kernel]` and `[limits]`, the `linux-2-6-32`, `yaffs2` and `gzip` downloads, the system toolchain | `out/system-kernel/zImage`, `Image`, `System.map`, `.config` |
| `userland` | `userland/`, the system toolchain and staging, `work/v8/` | `work/userland/system/`, `work/userland/system.log`; `out/install-tools/install-tools.tar` |
| `image` | `work/system/out/target`, `work/userland/system`, `work/v8/`, `out/system-kernel/zImage`, `out/install-tools/install-tools.tar`, `system/core-files.txt`, `system/image-accepted-undefined.txt`, `image/core/`, `image/overlay/`, `image/usbkey/`, the `image` downloads | `out/usbkey/` |
| `test` | The results of `system`, `v8`, `kernel` and `userland`, `tests/` | `work/tests/`, `work/sysroot-core`, PASS, FAIL and SKIP lines |

### check

This stage makes sure that the host and the repository are ready.

`scripts/host-prereqs.sh` checks the packages, the Python version and the user namespaces (see [Build host](#build-host)). `scripts/check-repo.py` checks the files that git tracks, and the files that are not tracked and not ignored:

- No two paths are different only in case.
- No symbolic links.
- Text files have LF line ends and no NUL bytes. `image/usbkey/autorun.sig` is the only file that keeps CRLF.
- Each file of LapinLibre has an `SPDX-License-Identifier` line in its first five lines: `GPL-2.0-only` in `kernel/`, `GPL-2.0-or-later` in `system/external/`, and `MIT` in all other folders. The list `UNMARKED` in the script gives the files that have no such line, and why (patches, licence texts, recorded test output, test data, JSON, and files whose readers take no comments).

### fetch

This stage gets all the pinned downloads, and checks each one.

`scripts/fetch.py` reads `[download.NAME]` in `config.toml`. There are two types:

| Type | Keys | How it is checked |
|---|---|---|
| File | `urls` (tried in sequence), `sha256`, optional `file` (the name in `dl/`; default: the last part of the first URL), `group` | The SHA-256 of the file must agree with `sha256`. |
| Git | `git` (the repository URL), `commit` (a full hash), `group` | The commit must be in the bare mirror `dl/git/NAME.git`. |

A file download goes to `NAME.part` first. The script tries each URL up to 3 times, with a longer wait after each failure. A file with the wrong checksum is deleted, and the build stops. A cached file with the wrong checksum is downloaded again. A git download is cloned one time; the script gets the commit only if the mirror does not have it.

| Command | What it does |
|---|---|
| `fetch.py NAME...` | Gets each download and prints its path. |
| `fetch.py --group GROUP` | Gets each download of the group. |
| `fetch.py --all` | Gets all the downloads. |
| `fetch.py --list` | Lists the downloads: name, group, first URL. |
| `fetch.py NAME --export DIR` | Writes the tree of a git download's commit into `DIR`. |

| Group | Downloads | Used by |
|---|---|---|
| `system-source` | Buildroot 2026.02.3, Linux 2.6.32.71 | `system`, `kernel` |
| `system` | Every Buildroot package download, and the downloads of LapinLibre's own packages | `system` |
| `v8` | V8 2.5.9.23 | `v8` |
| `kernel` | YAFFS2 (git), GNU gzip 1.3.12 | `kernel` |
| `image` | tzdata and tzcode 2026e, the RT73 Wi-Fi firmware and its licence | `image` |

`system/pin-downloads.py SHOW_INFO DL_DIR > pins.toml` writes the `group = "system"` entries. `SHOW_INFO` is the JSON of Buildroot's `make show-info`, and `DL_DIR` is a download folder that Buildroot filled and checked with its own `.hash` files. Each entry has the file below `br2026/`, its SHA-256, and its URLs: Buildroot's sources mirror first, then the sites that Buildroot names. The script leaves out the files that `config.toml` already pins. It lists on standard error the files that are missing, and the files that no package claims.

`config.toml` also holds `[key.md5]`: the MD5 of the files that the key must carry without change (see [The USB key](#the-usb-key)).

### toolchain

This stage gets the cross toolchain that the other stages use: GCC 14.3.0 and binutils 2.44 with uClibc-ng 1.0.57 (NPTL threads, wide characters, C++) and the headers of Linux 2.6.32.71, for the ARM920T with EABI and soft float.

`config.toml` `toolchain.name` names the toolchain, for example `toolchain-gcc14.3.0-uclibc-ng1.0.57-1`. It is also the tag of the toolchain's release and the name of its file, `NAME.tar.gz`.

How the stage works (`system/toolchain/build.sh`):

1. It chooses how to get the toolchain. If `config.toml` has `[download.toolchain]`, it fetches that file. If not, or if `LAPIN_TOOLCHAIN=build` is set, it builds the toolchain.
2. If the stamp `toolchain` holds the same key and `work/toolchain/sdk/bin/arm-buildroot-linux-uclibcgnueabi-gcc` exists, the stage stops. The key is the name and the checksum of the download, or for a build the name, a hash of `system/toolchain/`, the uClibc-ng patches and options, and Buildroot's checksum.
3. To build: it unpacks Buildroot in `work/toolchain`, writes `configs/lapin_toolchain_defconfig` from `system/toolchain/buildroot.config`, and runs `make sdk`. The log is `work/toolchain/out/build.log`, and Buildroot's step lines are shown. It copies the SDK to `out/toolchain/NAME.tar.gz` and prints its size and SHA-256.
4. It unpacks the file into `work/toolchain/sdk`, without its top folder, and prints the compiler's version.

`system/toolchain/buildroot.config` has the same CPU and toolchain lines as the system build had when Buildroot built its own toolchain. It selects no package and no root file system.

The published toolchain is built on GitHub's `ubuntu-24.04` runner, so its programs need the C library of Ubuntu 24.04 or newer on the build host. On an older host, set `LAPIN_TOOLCHAIN=build` to build the toolchain there. The compiler finds its files relative to its own folder, so the toolchain works from any work location.

### system

This stage builds the rabbit's packages with Buildroot 2026.02.3. Its toolchain is the `toolchain` stage's, in `work/toolchain/sdk`, which `system/buildroot.config` declares as an external toolchain (`BR2_TOOLCHAIN_EXTERNAL_CUSTOM`, with the features the toolchain has). Buildroot checks the declared features against the toolchain, and copies its C library into the target. An external toolchain gives the target only its libraries, so `system/external/post-build.sh` (`BR2_ROOTFS_POST_BUILD_SCRIPT`) installs uClibc-ng's `ldconfig` and `getconf` from the toolchain's sysroot, stripped as Buildroot strips the target. The rabbit's `linuxrc` makes the loader cache with `ldconfig`.

How the stage works (`system/build.sh`):

1. It calculates two keys. `key` is a hash of all of `system/`, `config.toml` and the stamp of `toolchain`. `build_key` is a hash of the inputs of Buildroot only: `system/buildroot.config`, `system/patches`, `system/external`, `config.toml` and the stamp of `toolchain`. So a different toolchain builds all the packages again.
2. If the stamp `system` holds `key` and `work/system/out/target/usr/lib` exists, the stage stops. It is up to date.
3. It gets Buildroot, Linux 2.6.32.71 and the `system` group into `dl/`.
4. If `work/system/.prepared` holds `build_key`, it continues the interrupted build. If not, it removes `work/system`, unpacks Buildroot, and writes `configs/lapin_system_defconfig` from `system/buildroot.config`, with `@DL_DIR@` set to `dl/br2026`, `@PATCH_DIR@` set to `system/patches` and `@TOOLCHAIN@` set to `work/toolchain/sdk`. Then it runs `make lapin_system_defconfig` with `BR2_EXTERNAL=system/external`.
5. It makes sure that each `BR2_...=y` line of `system/buildroot.config` is also in Buildroot's `.config`. A symbol that Buildroot did not enable stops the build.
6. It runs `make` and writes the log to `work/system/out/build.log`. It also shows Buildroot's step lines (for example `>>> mpd 0.23.15 Building`), about 900 lines for a full build, so you can see the progress. If `make` fails, it shows the last 40 lines of the log.

Buildroot gets no file from the network. `BR2_PRIMARY_SITE` is `http://127.0.0.1:9/` and `BR2_PRIMARY_SITE_ONLY=y`. Thus a download that is not pinned in `config.toml` stops the build.

The configuration (`system/buildroot.config`) enables these groups of packages:

| Group | Packages |
|---|---|
| Root tools | BusyBox 1.37 (configuration `system/external/busybox.config`), bash, mtd-utils, wireless tools, e2fsprogs, util-linux (libuuid and programs), strace, netcat, iw, wpa_supplicant (WEXT and nl80211), Dropbear, mdnsd (with the SSH service), liblockfile and lockfile-progs, GnuPG 1.4 (AES and RSA), crda and wireless-regdb |
| Libraries | libiconv, GNU gettext (`BR2_SYSTEM_ENABLE_NLS=y`, for `libintl.so.8`), ncurses, zlib, OpenSSL (with the `openssl` program), libcurl, ca-certificates (the image keeps its hash links in `/etc/ssl/certs`, each as a file, and its bundle file), D-Bus, dbus-glib, expat, libxml2, GLib, libgcrypt, libgpg-error, libnl, libjpeg-turbo (with its tools), libpng |
| Audio and video | FLAC, libogg, libvorbis, Tremor, libtheora, Speex, SpeexDSP, libmad, madplay, libid3tag, libmms, FFmpeg (a selected set of audio decoders, demuxers, parsers and protocols, `pcm_s16le` and `wav` out, and no devices) |
| LapinLibre's own packages | See the next table |

The uClibc-ng configuration fragment (`system/external/uclibc-ng.fragment`) sets `LDSO_CACHE_SUPPORT=y` and `LDSO_BASE_FILENAME="ld.so"`. Without the cache, the loader does not read `/etc/ld.so.conf` and does not find `/usr/karotz/lib`, and the daemons do not start.

LapinLibre's own Buildroot packages are in `system/external/package/` (the external tree `LAPINLIBRE`). They keep libraries at the old versions that the rabbit's programs use:

| Package | Version | Patches in the package |
|---|---|---|
| `protobuf-karotz` | protobuf 2.3.0 | `message-cc-includes` |
| `libmpd-karotz` | libmpd 0.17.0 | `karotz`, `select-eintr` |
| `mpd-karotz` | mpd 0.15.12 | `count-external-client`, `ffmpeg-6.1` |
| `libosip2-karotz` | libosip2 3.5.0 | none |
| `libexosip2-karotz` | libeXosip2 3.5.0 | none |
| `linphone-karotz` | linphone 3.4.1 | none |
| `python24-karotz` | Python 2.4.5 | six patches for the cross-compile, the modules and the target |
| `dbus-python-karotz` | dbus-python 0.83.0 | `fix-python-configure-check` |
| `simplejson-karotz` | simplejson 2.0.7 | none |
| `glib-compile-schemas-karotz` | `glib-compile-schemas` on the target | none |

These packages use `KAROTZ_ERA_CFLAGS` from `system/external/package/karotz-era.mk.inc`: `-std=gnu11` and flags that make GCC 14's new errors into warnings again (implicit function declarations, incompatible pointer types, int conversion, return mismatch, implicit int).

The patches to Buildroot's own packages are in `system/patches/` (`BR2_GLOBAL_PATCH_DIR`):

| Package | Patch | Why |
|---|---|---|
| crda | `0001-wireless-regdb-signing-key.patch` | Adds the key that signs the current `regulatory.bin`. Without it, crda refuses the database. |
| libffi | `0001-arm-call-without-blx-on-armv4t.patch` | The ARM920T has no BLX instruction. Without the patch, `ffi_call` (and thus dbus-glib) stops with SIGILL. |
| uclibc | `0001` to `0004` | `adjtimex`, `getrandom`/`getentropy`, `statx` and `accept4` only when the 2.6.32 kernel headers have the system call. |
| util-linux | `0001-fiemap-extent-shared-for-old-headers.patch` | Defines `FIEMAP_EXTENT_SHARED`, which the 2.6.32 headers do not have. |

### v8

This stage builds V8 2.5.9.23 as `libv8.so` for the rabbit, with the system toolchain.

`system/v8/build.sh` does not use SCons. It does the same steps as V8's `SConstruct` for a shared library, `arch=arm`, `mode=release` and no snapshot:

1. It unpacks the source into `work/v8/src` and applies `system/v8/patches/` (`js2c` on Python 3; `platform-linux.cc` on uClibc).
2. `tools/js2c.py` makes `libraries.cc` from the JavaScript library files of `SConscript`.
3. It compiles the `all` and `arch:arm` sources of `SConscript`, `platform-linux.cc`, `platform-posix.cc`, `snapshot-empty.cc` and `libraries.cc`, with `LAPIN_JOBS` jobs. `-ffile-prefix-map` makes the source paths in V8's assertion texts relative (`./src/...`).
4. It links `work/v8/libv8.so` (no soname) and the sample shell `work/v8/v8-shell`, which becomes `/usr/bin/v8-shell`.

The flags are V8's, for `-march=armv4t -marm -msoft-float`, plus these:

| Flag | Why |
|---|---|
| `-mno-thumb-interwork` | V8 stops on Thumb interworking below ARMv5T. |
| `-fno-delete-null-pointer-checks` | V8 tests `this == NULL`. Without the flag, every script crashed at start. |
| `-fno-strict-aliasing` | V8 reads and writes tagged pointers through other types. Without the flag, the garbage collector read a bad map word. |
| `-Wno-...` (a list) | Turns off the warnings that GCC 14 adds, so that V8's `-Werror` can stay. |

The stamp `v8` includes `system/v8`, `config.toml`, the compiler and the stamp of `system`.

### kernel

This stage builds the rabbit's kernel on Linux 2.6.32.71 with the system toolchain.

`system/kernel/build.sh` prepares the tree in `work/system-kernel/linux-2.6.32.71`:

1. It unpacks Linux 2.6.32.71 and applies `system/kernel/linux-2.6.32-modern-gcc.patch`.
2. It copies `kernel/src/` (the RABBITV3 board and its drivers) over the tree.
3. It applies `system/kernel/patches/*.patch` in sequence: the rabbit's character drivers, the I2S data pin, the camera, the platform devices, the board, the USB gadget and framebuffer, the rt2x00 timeout, the I2C delay, the NAND timings, the mac80211 scan time, the UDA134x codec, the s3c-hwmon build, the 2.6.32 APIs for `kernel/src`, and the time of the built-in initramfs.
4. It sets the machine name in `MACHINE_START` from `config.toml` `kernel.machine`.
5. It exports the pinned YAFFS2 commit and runs `kernel/yaffs.py YAFFS_TREE LINUX_TREE`. The script copies the YAFFS2 sources into `fs/yaffs2` and adds the folder to `fs/Kconfig` and `fs/Makefile`.
6. It writes `.config` from `system/kernel/rabbitv3_defconfig`, with `CONFIG_LOCALVERSION` from `kernel.localversion`.
7. It runs `make oldconfig`. If `oldconfig` asks a new question, the build stops: update `rabbitv3_defconfig`.
8. It runs `make zImage` and copies `zImage`, `Image`, `System.map` and `.config` to `out/system-kernel/`.
9. It makes sure that `zImage` is not larger than `limits.kernel_bytes`, and prints the `Linux version` line.

What the build fixes, so that the result does not depend on the build host:

| Item | How |
|---|---|
| The source path | `scripts/fixed-root.sh` runs the compile in a user and mount namespace, with the tree at `kernel.build_path` (`/build/karotz-kernel`). |
| The user and host in the banner | Commands `whoami` and `hostname` first on `PATH`, which print `kernel.build_user` and `kernel.build_host`. |
| The compressor | GNU gzip 1.3.12, built from the pinned source, first on `PATH` as `gzip`, with `-n`, so the compressed kernel's header has no time. |
| The version suffix | `LOCALVERSION` is `-` and the build version (see [Build id and version](#build-id-and-version)), so `uname -r` is for example `2.6.32.71-sweetlilmre-v0.1.0`. |
| The build number | `KBUILD_BUILD_VERSION` is always `1`. |
| The banner's date | `KBUILD_BUILD_TIMESTAMP` is the build version and the commit's date, for example `v0.1.0 (2026-10-24)`. |
| The compile's own dates | `SOURCE_DATE_EPOCH` is the commit's time (or the value already set), for `__DATE__` and `__TIME__` (YAFFS2's "yaffs built" message) and for the built-in initramfs (`system/kernel/patches/91-initramfs-mtime.patch`). |

So two builds of the same commit give the same `zImage`. A release kernel's banner is for example `Linux version 2.6.32.71-sweetlilmre-v0.1.0 (sweetlilmre@sweetlilmre-builder) (gcc version 14.3.0 (Buildroot 2026.02.3) ) #1 v0.1.0 (2026-10-24)`.

The stamp `system-kernel` includes `system/kernel`, `kernel/src`, `kernel/yaffs.py`, `config.toml`, the compiler, the stamp of `system`, the build version and the commit's date.

### userland

This stage builds LapinLibre's programs and the install tools of the key.

`system/userland.sh` runs `make -C userland all player` with the system toolchain, the system build's `pkg-config` and `protoc-2.3.0`, the host's `dbus-binding-tool` and `glib-genmarshal`, and the V8 headers and library. It removes the old output first. The output is in `work/userland/system`, and the log is `work/userland/system.log`. The programs are built with debug information, which stays in `work/userland/system` for debugging; `-ffile-prefix-map` turns the repository's and the work location's paths into relative ones (`./`), in the debug information and in `__FILE__`, so the code doesn't depend on where the build ran.

| Output folder | Contents | Place in the core |
|---|---|---|
| `rootfs/` | `power`, `ears`, `led` | `/karotz/bin` |
| `usr/` | `immortaldog` and the daemons: `button-daemon`, `ears-daemon`, `led-daemon`, `rfid-daemon`, `scheduler-daemon`, `webcam-daemon`, `serial-daemon`, `controller`, `multimedia-daemon`, `voice-daemon`, `k2k-daemon`, `karotzVM` | `/usr/karotz/bin` |
| `usr/bin/` | `karotz-player` | `/usr/bin` |
| `usr/lib/` | `libkarotz-common.so`, `libkarotz-messages.so` | `/usr/karotz/lib` |
| `usr/scripts/k2k/` | `playback`, `rec` | `/usr/scripts/k2k` |

`userland/Makefile` generates each daemon's D-Bus server code from `userland/dbus/*.xml`, the signal marshallers from `karotz-marshal.list`, and the protobuf code from `userland/proto/*.proto`. Without `CROSS`, it builds for the host and leaves out the programs that need libraries of the system build only (`multimedia-daemon`, `voice-daemon`, `k2k-daemon`, `karotzVM`, `karotz-player`).

`system/install-tools.sh` builds `madplay` and `ears` as static programs for the key's install, which runs them from `/tmp` while `/usr` is erased. It copies Buildroot's patched source trees of zlib 1.3.2, libid3tag 0.16.3, libmad 0.15.1b and madplay 0.15.2b, and builds them static, with OSS output and without NLS. `ears` comes from `userland/src/rootfs/ears/ears.c`. The script makes sure that neither program needs a library or a loader. It writes `out/install-tools/install-tools.tar` (the two programs, owner root, mode 0755, time `SOURCE_DATE_EPOCH` or 0), and prints the sizes and the MD5.

### image

This stage makes the USB key in `out/usbkey` (`image/tools/image.py [--out DIR]`).

#### The core

The core is the root filesystem and `/usr` that the key installs. `make_core` makes it in this sequence:

1. It reads `system/core-files.txt`. Each line is one entry, as the next table shows.
2. It checks the libraries and the symbols (see the list below).
3. It checks the files that `config.toml` `[key.md5]` pins with a `part:path` name (the keyring in `rootfs:karotz/etc/gpg/`).
4. It removes `usr:etc/ld.so.cache`. `linuxrc` makes a new one at boot.
5. It applies `image/overlay/rootfs` and `image/overlay/usr`.
6. It writes the build version to `rootfs:karotz/etc/lapinlibre-build`.

| Entry in `core-files.txt` | Meaning |
|---|---|
| `part:path f MODE UID:GID SOURCE` | A file. `part` is `rootfs` or `usr`. |
| `part:path l TARGET` | A symbolic link. |
| `part:path d MODE UID:GID` | A folder. |
| `part:path c\|b MODE UID:GID RDEV` | A device node (root filesystem only). |

| File source | Where the data comes from |
|---|---|
| `target` | The system build's target at the same path (`usr:P` is `usr/P` in the target). |
| `target:PATH` | The target's file at `PATH`. |
| `own` | LapinLibre's build: the userland output, `libv8.so` and `v8-shell`, stripped as they go into the image (the host's `arm-linux-gnueabi-strip --strip-unneeded`, as Buildroot strips its packages). Each such file must be in the list. |
| `core` | `image/core/<part>/<path>` in the repository. A script with CRLF stops the build. |
| `generated` | Made by `image.py`. Each generated file must be in the list. |

The library and symbol checks (`image/tools/system.py`):

1. Each ELF file's libraries and loader must resolve. The root's programs use only the root's `/lib`, because they run before `/usr` is mounted, and during an install that erases `/usr`. Programs in `/usr` use their RPATH, `/usr/karotz/lib`, `/lib` and `/usr/lib`. A library that is not in the list but is in the target is added (to `/lib` if a root program needs it, if not to `/usr/lib`) and reported. A library that the target does not have stops the build.
2. A library in `/lib`, `/usr/lib` or `/usr/karotz/lib` that nothing uses stops the build. `libgcc_s.so.1` (loaded with `dlopen`) and LapinLibre's own libraries are exceptions.
3. Each undefined dynamic symbol of each ELF file must be defined in the libraries that it resolves to. Python 2.4's modules can also use the symbols of `/usr/bin/python2.4`. `system/image-accepted-undefined.txt` lists exceptions, as `file name: symbol ...: reason` (it has no entries now).

These checks use `arm-linux-gnueabi-readelf` and `arm-linux-gnueabi-nm` from the host.

An overlay folder (`image/tools/fstree.py`) can have `files/`, which is copied over the part, and `ops`, with one operation on each line: `rm`, `mkdir`, `symlink`, `chmod`, `chown`. A replaced file keeps its mode and owner. A new file gets mode 0755 if it starts with `#!`, and 0644 if not. The only overlay file now is `image/overlay/usr/files/etc/conf/karotz-player.conf`.

#### Generated files

`image.py` makes these files of the core (`core_generated`). [sound-map.md](sound-map.md) tells when the rabbit plays each sound.

| File | How it is made |
|---|---|
| `rootfs:karotz/etc/inetd.conf` | Text: BusyBox `httpd` on port 80, at most 60 connections a minute, with the configuration that `inetd.sh` makes in RAM. |
| `rootfs:lib/firmware/rt73.bin` and `LICENCE.ralink-firmware.txt` | The pinned downloads `rt73-firmware` and `rt73-licence`. |
| `usr:etc/timezone.list` | `image/tools/tzlist.py`: one `<zone>;<POSIX TZ>` line for each zone and link of the pinned tzdata, compiled with the pinned tzcode's `zic`. Rules that uClibc-ng cannot read are replaced. The reference date is the release date of tzdata, so the list depends only on the pins. |
| `usr:karotz/sounds/boot-ready.wav`, `button-click.wav`, `button-double.wav`, `button-triple.wav`, `button-long.wav` | Tones (`SOUNDS`): 16-bit mono WAV at 22050 Hz, each note a sine that fades. |
| `usr:karotz/sounds/wakeup.mp3`, `notify.mp3`, `acknowledge.mp3`, `shutter.mp3`, `error.mp3` | Tones (`EFFECTS`; `error.mp3` is `ERROR_SOUND`, also the key's), encoded to MP3 by the host's `ffmpeg` (64 kbit/s, no tags, bit-exact). |

The spoken sounds are not made by the build. They are kept in the repository (`image/core/usr/karotz/sounds/*.mp3` and `image/usbkey/sound/*.mp3`). `image/tools/speech_sounds.py` and `image/tools/install_prompts.py` made them one time, with their own virtual environments.

#### Images and limits

| Image | Format | Limit in `config.toml` `[limits]` |
|---|---|---|
| `zImage` | The kernel from `out/system-kernel/zImage` | `kernel_bytes` = 3145728 (the `mtd1` partition) |
| `rootfs.img.gz` | cramfs (version 2, little-endian, `image/tools/cramfs.py`), compressed with gzip level 9, internal name `rootfs.img` | `rootfs_gz_bytes` = 5242880 (the `mtd2` partition) |
| uncompressed root filesystem | The cramfs image before gzip | `rootfs_bytes` = 5242880 (the 5 MiB RAM disk `ram0`) |
| `usr.tar.gz` | A tar archive of `/usr`, compressed with gzip level 9 | none |

A larger image stops the build. The gzip time stamps are `SOURCE_DATE_EPOCH`, or the time of the last commit.

#### Build id and version

- The build version is `git describe --tags --match 'v[0-9]*' --dirty --always --abbrev=12`, which counts only release tags (the toolchain's tags are left out): the release tag on the commit (`v0.1.0`), or else the last tag, the number of commits after it and the commit (`v0.1.0-3-g898dd4892f69`), or else the commit when there is no tag. `-dirty` is added if tracked files have changes. Without git, it is `unversioned`. The image writes it to `/karotz/etc/lapinlibre-build` and `install.conf`. The web page, `get_version`, the boot log and the kernel's `uname -r` show it.
- Release tags are `v` and a SemVer version: `v0.1.0`.
- The build id is the first 16 hexadecimal characters of the SHA-256 of the SHA-256 digests of `rootfs.img.gz`, `usr.tar.gz` and `zImage`, in that sequence.

#### The USB key

`image.py` reads all the inputs before it removes the old key. It does not replace a folder that is not empty and has no `build_id` file. The build does not read `.env`.

| File in `out/usbkey` | Contents |
|---|---|
| `zImage`, `rootfs.img.gz`, `usr.tar.gz` | The images. |
| `install.conf` | `BUILD_ID`, `BUILD_VERSION`, `KERNEL_MD5`, `ROOTFS_MD5`, `USR_MD5`, `INSTALL_TOOLS_MD5`, one `NAME=value` line each. `autorun` reads it. |
| `build_id` | The build id. |
| `autorun`, `install-lib.sh`, `applysettings.sh`, `lapinlibre.txt` | Copied from `image/usbkey`. A file with CRLF stops the build. `autorun` and the `.sh` files get mode 0755. |
| `autorun.sig` | Copied from `image/usbkey` without change. Its MD5 must agree with `config.toml` `[key.md5]`, because the rabbit's factory system runs a key's `autorun` only when `gpg --verify autorun.sig` passes. |
| `install-tools.tar` | From `out/install-tools/`. |
| `sound/key-found.mp3`, `sound/install-busy.mp3`, `sound/error.mp3` | Tones (`INSTALL_SOUNDS`; `error.mp3` is the same as `/usr`'s), encoded to MP3. |
| `sound/checks-start.mp3`, `checks-passed.mp3`, `checks-failed.mp3`, `install-start.mp3`, `install-done.mp3` | Spoken prompts, copied from `image/usbkey/sound`. |

At the end, `image.py` prints the build id, the size and MD5 of each image, and notes on the core (libraries that it added, the time-zone report, overlay changes).

### Outputs

This section gives all the results in `out/`.

| Path | Made by | Contents |
|---|---|---|
| `out/usbkey/` | `image` | The USB key (see [The USB key](#the-usb-key)). |
| `out/system-kernel/` | `kernel` | `zImage`, `Image`, `System.map`, `.config`. |
| `out/install-tools/install-tools.tar` | `userland` | Static `madplay` and `ears` for the install. |

### The image check

This section tells what `system/check-image.sh` tests.

`system/check-image.sh [KEY]` tests the files of a built key (default `out/usbkey`). It is not a stage of `build.sh`. It runs `image/tools/sysroot.py sysroot-image KEY`, which writes the key's `rootfs.img.gz` and `usr.tar.gz` to `work/sysroot-image` as the rabbit mounts them. It sets up `/etc` as the boot does, and runs the key's own `ldconfig` under qemu-user to make `ld.so.cache`. Then it runs these checks under `qemu-arm -cpu arm926`:

| Check | Pass condition |
|---|---|
| `ld.so.cache` | The loader finds `libkarotz-common.so` in `/usr/karotz/lib` for `controller`, through the cache only. |
| `flash_erase` | `--version` names mtd-utils. |
| `karotz-player` | It decodes `boot-ready.wav` up to the audio device, which qemu does not have. |
| `openssl`, `timeout`, `bash`, `gpg`, `busybox`, `mpd` | Each program prints its version or help (GnuPG 1.4, BusyBox 1.37, mpd with the FFmpeg plug-in). |
| `python` | `python2.4` imports `dbus`, `simplejson` and the standard modules. |
| Behaviour scenarios | The same scenarios as `tests/run.sh`, on the key's sysroot, with the label `image/`. |

`system/tests/core-boot.sh [KEY]` tests the key's boot files under qemu-user: the absolute paths in the boot files, the syntax of each boot script with the image's BusyBox `ash`, the system bus and its policy, Dropbear with a public key and without a password, and `telnetd`. Neither `tests/run.sh` nor `check-image.sh` runs it.

### GitHub workflows

This section tells what the workflows in `.github/workflows` do. They use only GitHub's own actions (`actions/checkout`, `actions/cache`, `actions/upload-artifact`), pinned by commit, and the `gh` command of the runner.

| Workflow | When | What it does |
|---|---|---|
| `check` | Each push to a branch, and each pull request | Runs `scripts/check-repo.py` and `tests/image/test_fstree.py`. These need no build, and take less than a minute. |
| `release` | A pushed tag `v*` | Builds the stages `check fetch toolchain system v8 kernel userland image` on `ubuntu-24.04` from the tagged commit, zips `out/usbkey`, and makes the release with the tag's name and the files `LapinLibre-VERSION.zip` and `LapinLibre-VERSION.zip.sha256`. |
| `toolchain` | "Run workflow" on GitHub | Builds the toolchain from source (`LAPIN_TOOLCHAIN=build ./build.sh check fetch toolchain`) and makes the release `toolchain.name` with `NAME.tar.gz` and its SHA-256, not marked as the latest release. It stops if that release exists. See [Publish a new toolchain](#publish-a-new-toolchain). |
| `release` | "Run workflow" on GitHub | The same build and zip from the chosen branch. The zip is kept with the run for 7 days. Nothing is published. |
| `downloads` | A push to `main` that changes `config.toml`, and "Run workflow" on GitHub | Fetches every pinned download (`./build.sh fetch`) into the cache the `release` and `toolchain` workflows restore, saved under `main`. A cache belongs to the branch or tag of the run that saved it, and a run can only restore its own and `main`'s, so a cache saved by one release tag's run is never seen by the next tag's. With the key already cached it stops after the restore. |

The `release` workflow:

- Stops if the build version ends in `-dirty`, if a tag's build version is not the tag, or if the key's `BUILD_VERSION` is not the build version.
- Puts the work location in the runner's temporary folder (`$RUNNER_TEMP/lapinlibre`).
- Removes the runner's unused SDKs, to make space for the work location (about 13 GB with the downloads), and prints the free space of `/` and `/mnt` before and after. It lets unprivileged users make user namespaces.
- If the build fails, keeps the stages' logs (`work/**/*.log`) with the run for 7 days.
- Installs the packages with `scripts/host-prereqs.sh --install`, after removing `azure.archive.ubuntu.com` from the runner's mirror list (`/etc/apt/apt-mirrors.txt`). That mirror can trickle for many minutes without apt moving to the next one ([actions/runner-images#14823](https://github.com/actions/runner-images/issues/14823)).
- Restores `dl/` from the workflow cache, with a key from the hash of `config.toml`; the `downloads` workflow fills it from `main`. The other stages start from nothing, so `system` takes most of the time.
- Does not run the tests. Run them on the build host before you push the tag (see [Make a release](#make-a-release)).

### Test suites

This section gives the test suites and how `tests/run.sh` runs them.

`tests/run.sh` writes each section's output to the terminal and to `work/tests/run.log`. Each test prints `PASS`, `FAIL` or `SKIP` (or `RECORDED` with `--record`). The summary counts them. The exit status is not zero if a test failed.

| Command | What it runs |
|---|---|
| `bash tests/run.sh` | All the sections in the next table. |
| `bash tests/run.sh --record` | All sections, but the scenarios write `tests/expected/` and do not compare. The two system tests do not run. |
| `bash tests/run.sh NAME...` | The sysroot, the preloads and the named scenarios only. |

| Section | Files | What it tests |
|---|---|---|
| Image tests | `tests/image/run.py` runs `test_*.py` | `test_fstree.py`: the cramfs and tar codecs and overlays. `test_usbkey.py`: builds a key with test network settings in a temporary folder, and runs its real `autorun` against a simulated rabbit, with flashing and reboot replaced by records; it also compares the key's layout with `tests/expected/key-files.txt`. `test_core_boot.py`: the generated files, `inetd.sh` and the telnet switch, `usbkey.sh`, `network.sh` and the head button's watcher. `test_tzlist.py`: the time-zone list, compared with tzdata under qemu. `test_web_api.py`: the web API's CGI programs and the image's BusyBox `httpd`. |
| Test preloads | `tests/fakes/` | Builds `devtrace.so` (host) and `ioctlfake-arm.so` and `timeshift-arm.so` (ARM) into `work/tests`. |
| App server tests | `tests/app_server/test_app_server.py` | `tools/app-server` against a fake controller. See [app-server.md](app-server.md). |
| Sysroot | `image/tools/sysroot.py core` | Makes `work/sysroot-core`: the core as `make_core` builds it, with `ld.so.cache` and an empty `/var`. |
| Behaviour scenarios | `tests/scenarios/`, `tests/expected/` | Each program's record, compared with `tests/expected/NAME.txt`. |
| Aggressive tests | `tests/aggressive/` | Hostile and broken input. Each module's `check(record)` gives the problems. |
| libmpd retries on EINTR | `system/tests/libmpd-eintr.sh` | libmpd gets the status from a slow MPD while a timer signal arrives every 0.2 s. |
| mpd-karotz | `system/tests/mpd-karotz.sh` | The system build's mpd: tags, playback of six formats compared with the host's `ffmpeg`, a seek, and `count_externalClient`. |

The behaviour scenarios that `tests/run.sh` runs: `button_daemon`, `ears_daemon`, `led_daemon`, `rfid_daemon`, `scheduler_daemon`, `webcam_daemon`, `webcam_capture`, `serial_daemon`, `controller`, `controller_platform`, `controller_webcam_direct`, `controller_message_query`, `controller_schedule_gone`, `controller_k2k_no_config`, `controller_bad_descriptor`, `app_server`, `multimedia_daemon`, `voice_daemon`, `k2k_daemon`, `karotzVM`. `tests/expected/README.md` tells about the parts of some records.

`tests/scenarios/button_replay.py` replays a file of button reads recorded on a rabbit (`KAROTZ_BUTTON_REPLAY`), so `tests/run.sh` does not run it. The aggressive test `button_bounce` replays the recordings in `tests/data/button/`.

Tests that are not in `tests/run.sh`:

| Test | Where it runs |
|---|---|
| `system/check-image.sh` | The build host, on a built key. |
| `system/tests/core-boot.sh` | The build host, on a built key. |
| `tests/device/immortaldog.sh` | The rabbit (from `/dev/shm`) or natively. qemu-user runs `vfork()` as `fork()`, so it cannot test `immortaldog`. |
| `tests/device/web-api.sh` | The rabbit: a second `httpd` on 127.0.0.1:8099, with copies of the new files in RAM. The options `hardware`, `tag` and `reboot` use the rabbit's hardware. See [web-api.md](web-api.md). |

### The harness

This section tells how `tests/harness.py` runs a program and makes its record.

```
python3 -I -B tests/harness.py [--record] [--suite scenarios|aggressive] --sysroot SYSROOT
    --build BUILD --fakes FAKES [--expected DIR] [--expected-build DIR] [--label LABEL] NAME...
```

| Option | Meaning |
|---|---|
| `--suite` | `scenarios` (default) or `aggressive`: the package that holds the modules. |
| `--sysroot` | The sysroot for qemu (`work/sysroot-core` or `work/sysroot-image`). |
| `--build` | The userland build (`work/userland/system`). An ARM build runs under qemu-arm. A host build runs natively. |
| `--fakes` | The folder of the test preloads (`work/tests`). |
| `--expected` | The folder of the expected records (default `tests/expected`). |
| `--expected-build` | A second folder of records. A record there replaces the one in `--expected`. |
| `--label` | A prefix for the result lines, for example `image/`. |
| `--record` | Writes the records and does not compare. It has no effect on aggressive tests. |

For each name, the harness does these steps:

1. It makes a temporary working folder `lapinlibre-<daemon>-...`.
2. It starts a private `dbus-daemon` as the system bus, with a policy that allows all and `ANONYMOUS` authentication (the program claims uid 0 in its user namespace). `dbus-monitor` records the signals.
3. It makes each device of `DEVICES` as a FIFO, or as a regular file for `REGULAR_FILE_DEVICES`. For an ARM build, the device goes in the sysroot's `/dev`, because `qemu-arm -L` uses the sysroot's path first.
4. It starts the program in new user, mount, network and PID namespaces (`unshare -r -m -n -p -f --mount-proc`), with only loopback up. The program is a child of a shell, not PID 1.
5. It calls `drive(daemon, bus)`, then adds the signals, the device I/O and whether the program still runs.
6. It stops the program with SIGTERM (SIGKILL after 20 s) and compares the record, or writes it.

What the harness puts in place of the rabbit:

| Fake | How it works |
|---|---|
| qemu-user | `qemu-arm -cpu arm926 -L SYSROOT`, with `LD_LIBRARY_PATH` set to the build's `usr/lib`, `/usr/karotz/lib`, `/usr/lib` and `/lib`. `-d strace` writes the system calls to the run's log. The harness reads the device I/O (`open`, `read`, `write`, `ioctl`, and `system` commands) from it. |
| Devices | FIFOs that the test writes to (`daemon.write(dev, data)`), or regular files where `read()` returns 0 when nothing is queued. |
| `ioctlfake-arm.so` | With `FAKE_IOCTL_TYPES`: the ioctls of those type bytes succeed on the FIFOs, and read ioctls return zeros, or the bytes in `KAROTZ_FAKE_IOCTL_DATA_FILE`. |
| `timeshift-arm.so` | `time()` adds the seconds in the file `KAROTZ_TIMESHIFT_FILE`, so a test can age karotzVM's reply handlers. |
| `devtrace.so` | For a host build: logs the device I/O in the same form, and logs `system()` without running it. |
| Stubs | `PATH` holds only commands that exit 0: `reboot`, `halt`, `poweroff`, `shutdown`, `killall`, `kill`, `madplay`, `python`, `karotz-player`, `rm`, `cp`, `mv`, `mkdir`, `touch`, `sync`, `ifconfig`, `route`, `udhcpc`, `wpa_supplicant`, `dbus-send`, `sleep`, `echo`, `mpd`, `flac`. The stubs are also mounted over `/sbin/reboot`, `halt`, `poweroff`, `shutdown`, `ifconfig` and `route`. |
| Time zone | `TZ=UTC0`. The sysroot's `/etc/TZ` is empty, which uClibc-ng reads as UTC. |
| Servers | Some scenarios start their own fakes: `fake_platform.py` (the platform server, on a veth pair), `karotzvm_controller.py` (the controller for karotzVM), and `conf_fixture.py` (fixed `karotz.conf` and `voice.conf`). |

The record is a list of lines:

1. The lines that `drive` returns.
2. `== signals`, then one `interface.member(args)` line for each signal.
3. `== device I/O`, then the device operations. A block of up to 8 lines that repeats the block before it is removed, because the number of polls depends on time.
4. `== daemon still running: True` or `False`.

Then the scenario's `normalise_record` runs, and the harness replaces paths that change from run to run: `<RUN>`, `<BUILD>`, `<SYSROOT>`, `<ROOT>`, `<WORK>`, `<HOME>`, the harness's pid in `/tmp` names (`<pid>`), and the folders of source file names.

A scenario that passes removes its working folder. A failed one keeps it, with `daemon.log`, `monitor.log` and `record.txt`, and prints up to 80 lines of diff.

### Scenario module

This section gives the names that a module in `tests/scenarios` or `tests/aggressive` can define.

| Name | Required | Meaning |
|---|---|---|
| `DAEMON` | Yes | The program in the build: in `usr/`, or in `rootfs/` if `usr/` does not have it. |
| `DEVICES` | Yes | The device names below `/dev`. |
| `drive(daemon, bus)` | Yes | Drives the program and returns the first lines of the record. `bus.send(...)` runs `dbus-send`, `bus.wait_for_name(name)` waits for a service, and `harness.introspection(bus, service, path)` lists an object's methods and signals. |
| `check(record)` | Aggressive tests only | Returns a list of problems. |
| `summary(record)` | No | Aggressive tests: one line printed with each result. |
| `FAKE_IOCTL_TYPES` | No | ioctl type bytes that succeed on the FIFOs, for example `[0x6B]` for SPI. |
| `REGULAR_FILE_DEVICES` | No | Devices that are regular files. |
| `sandbox_setup(workdir, sysroot)` | No | Returns shell commands that run in the program's namespaces before it starts. |
| `raw_device_filter(lines)` | No | Gets the device I/O before repeats are removed. Returns the lines to keep and extra record lines. |
| `normalise_record(record)` | No | Masks what changes from run to run. |
