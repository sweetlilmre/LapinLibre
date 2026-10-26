<!-- SPDX-License-Identifier: MIT -->
# Web page and HTTP API

The rabbit has a web page and an HTTP API. You use them from a browser, a script or a home-automation system on the local network. The page and the API are in `image/core/usr/www/`; the shared request code is in `image/core/usr/share/lapinlibre/`.

## How to

This section gives the procedures for the usual tasks. Replace `<password>` with your web password. If the rabbit has no web password, remove `-u admin:<password>` from the commands: the web server then asks for no login. Replace `lapinlibre` with the rabbit's name if you changed it (`HOSTNAME=` in `lapinlibre.txt`).

### Open the web page

1. Connect your computer to the same network as the rabbit.
2. Open `http://lapinlibre.local/` in a browser.
3. If the browser asks for a login, type the user `admin` and your web password.
4. If `lapinlibre.local` does not open, use the rabbit's IP address, for example `http://192.168.1.10/`.

### Set or remove the web password

1. Open `lapinlibre.txt` on the USB key.
2. Set `WEB_PASSWORD=` to a password of 8 to 64 characters. Do not use spaces, `"`, `\` or `:`.
3. Run the install from the key (see [install.md](install.md)).
4. Start the rabbit. At the start, the rabbit changes the password into a hash and deletes the plain text.
5. To remove the password, leave `WEB_PASSWORD=` empty and run the install again. The page then has no login.

### Change the LED colour

1. Open the web page.
2. In the LED section, select a colour.
3. Optional: select "Pulse", a second colour and a period in ms.
4. Click "Set the LED".

To do the same from a command line, type:

```sh
curl -u admin:<password> "http://lapinlibre.local/cgi-bin/leds?color=FF0000"
curl -u admin:<password> "http://lapinlibre.local/cgi-bin/leds?color=00FF00&pulse=1&color2=000000&speed=700"
```

### Make the rabbit speak

1. Make sure that a speech server is set (`SERVER=` or `TTS_SERVER=` in `lapinlibre.txt`).
2. Type this command:

```sh
curl -u admin:<password> --data-urlencode "text=Hello, I am awake." http://lapinlibre.local/cgi-bin/tts
```

3. To say the time, type:

```sh
curl -u admin:<password> http://lapinlibre.local/cgi-bin/apps/clock
```

### Play a sound or a radio stream

1. Get the names of the sounds:

```sh
curl -u admin:<password> http://lapinlibre.local/cgi-bin/sound_list
```

2. Play one sound by its name:

```sh
curl -u admin:<password> "http://lapinlibre.local/cgi-bin/sound?id=wakeup"
```

3. Play a stream by its URL:

```sh
curl -u admin:<password> --data-urlencode "url=http://radio.example/stream.mp3" http://lapinlibre.local/cgi-bin/sound
```

4. Stop the playback:

```sh
curl -u admin:<password> "http://lapinlibre.local/cgi-bin/sound_control?cmd=stop"
```

### Set up the clock, moods, stories, sounds and radio stations

The talking clock needs only a speech server. The other features play files that you put on the USB key. The rabbit has no files of its own for them.

1. For the clock, make sure that the rabbit has a speech server: `SERVER=` or `TTS_SERVER=` in `lapinlibre.txt` (see [install.md](install.md)). The clock says the time in the rabbit's language. You do not install other files for it.
2. Remove the USB key from the rabbit and put it in your computer.
3. Make these folders and files at the top of the key. Each one is optional:

| On the key | What to put in it | Played by |
|---|---|---|
| `moods/` | MP3 files, one for each mood, for example `1.mp3`, `2.mp3` or `happy.mp3`. | `apps/moods?id=<name>`; `apps/moods` with no `id` plays a random mood. |
| `moods/moods.txt` | Labels for the list, one line for each file: `name:label`, for example `1:Snoring`. | The web page and `moods_list`. |
| `stories/` | MP3 files, one for each story. | `stories?id=<name>`. |
| `stories/stories.txt` | Labels, as `moods.txt`. | The web page and `stories_list`. |
| `sounds/` | MP3 or WAV files. A file here hides a rabbit sound with the same name. | `sound?id=<name>`. |
| `radios.txt` | One station on each line: `name\|url`, for example `Jazz\|http://radio.example:8000/live.mp3`. Lines that start with `#` are comments. | `sound?url=<url>`; the web page lists the stations. |

4. Give each file a name of 1 to 64 letters, digits, `_` and `-`, then `.mp3` (or `.wav` in `sounds/`). The rabbit does not list or play other names. Names that are numbers come first in the lists, in numeric order.
5. Put the key back in the rabbit. The rabbit mounts it again, and the lists show the new files at once. You do not restart the rabbit.
6. Make sure that the files are there:

```sh
curl -u admin:<password> http://lapinlibre.local/cgi-bin/moods_list
curl -u admin:<password> http://lapinlibre.local/cgi-bin/stories_list
```

Music for the music player can be in any other folder of the key: mpd reads the whole key.

### Move the ears

1. Type this command (positions 0 to 16; one turn is 17 steps):

```sh
curl -u admin:<password> "http://lapinlibre.local/cgi-bin/ears?left=4&right=8"
```

2. To move the ears without the recalibration first, add `&noreset=1`.

### Learn an RFID tag and give it an action

1. Open the web page.
2. In the RFID tags section, click "Learn new tags". The nose pulses orange, and the section shows how long learning lasts.
3. Put the tag on the nose. The nose flashes green for a new tag and yellow for a known tag.
4. Click "Stop learning". Learning also stops after 2 minutes. While learning is on, no tag runs its action.
5. Click "Edit" next to the tag. The editor shows the tag's action, if it has one.
6. Select an action and fill in its details. For a URL request with a stored password, leave the password empty to keep it.
7. Click "Give the tag this action".
8. Put the tag on the nose. The nose flashes green and the rabbit does the action.

To give an action from a command line, encode the endpoint's path in base64:

```sh
curl -u admin:<password> --data-urlencode "tag=0123456789ABCDEF" \
     --data-urlencode "url=$(printf '%s' '/cgi-bin/leds?color=FF0000' | base64)" \
     http://lapinlibre.local/cgi-bin/rfid_assign_karotz
```

### Take a picture

1. Type this command:

```sh
curl -u admin:<password> http://lapinlibre.local/cgi-bin/snapshot
```

2. Read the `filename` in the answer.
3. Download the picture:

```sh
curl -u admin:<password> -o picture.jpg "http://lapinlibre.local/cgi-bin/snapshot_get?filename=snapshot_2026_10_23_08_15_00.jpg"
```

### Put the rabbit to sleep and wake it up

1. To put the rabbit to sleep, type `curl -u admin:<password> http://lapinlibre.local/cgi-bin/sleep`.
2. To wake it up, type `curl -u admin:<password> http://lapinlibre.local/cgi-bin/wakeup`.
3. To wake it up without the wake-up sound, add `?silent=1`.

### Read the logs

1. Type this command (`source` is `system`, `kernel` or `mpd`):

```sh
curl -u admin:<password> "http://lapinlibre.local/cgi-bin/lapinlibre/logs?source=system&lines=100"
```

### Copy an action's URL from the web page

1. Do the action on the web page.
2. Open "The URL of this action" below the result line in the same section.
3. Copy the URL into your script or home-automation system. Add the user and password, for example `http://admin:<password>@lapinlibre.local/cgi-bin/...`.

## Technical details

This section tells how the web server, the API and the web page work. It also gives the reference tables of all the endpoints.

### The web server

BusyBox `httpd` serves the page and the API. `inetd` starts it for each connection on TCP port 80, IPv4 only, with at most 60 connections a minute. The service line is in `/etc/inetd.conf`, which the image build writes (`image/tools/image.py`):

```
80 stream tcp nowait.60 root /bin/busybox httpd -i -c /var/run/httpd.conf -h /usr/www
```

The web root is `/usr/www`. The programs under `/usr/www/cgi-bin` are the endpoints. Each one is a shell script that sources `/usr/share/lapinlibre/web.sh`, which is outside the web root.

At each start, `/karotz/scripts/inetd.sh` writes the server's configuration in RAM, at `/var/run/httpd.conf`. The root file system is read-only, so the configuration cannot be on it. The configuration contains:

- `A:` lines for the local network only: `127.0.0.1`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` and `169.254.0.0/16`. Then `D:*` refuses all other clients.
- The index and type lines of the fixed file `/karotz/etc/httpd.conf` (`I:index.html`).
- With a web password: `/:admin:<hash>`. Then `httpd` asks every request for the user `admin` and the password. A request without the correct login gets HTTP 401.

The password goes through these steps:

| Step | Who | What |
|---|---|---|
| 1 | `applysettings.sh` (the install) | Checks `WEB_PASSWORD` (8 to 64 printable characters, no space, `"`, `\` or `:`) and writes it to `/usr/etc/conf/web_password` (mode 0600). An empty `WEB_PASSWORD` deletes the password and its hash. |
| 2 | `inetd.sh` (next start) | Makes a SHA-512 crypt hash (`busybox cryptpw -m sha512`) in `/usr/etc/conf/web_password.hash`, then deletes the plain file. The flash is written once for each new password. |
| 3 | `inetd.sh` (each start) | Puts the hash in `/var/run/httpd.conf`. |

If the hash cannot be made, or the hash file is damaged, the configuration is only `D:*` and the page is locked. If no password is set, the page has no login and the system log says so.

The rabbit's name comes from `/usr/etc/conf/hostname` (`HOSTNAME=` in `lapinlibre.txt`); the default is `lapinlibre`. `mdnsd` answers to `<name>.local` on `wlan0` (`/usr/etc/init.d/S40lapinlibre`).

### The checks of every request

`web_begin` in `web.sh` does these checks in this sequence before the endpoint does anything. A failed check ends the request.

| # | Check | Refusal |
|---|---|---|
| 1 | With a web password, `REMOTE_USER` must be `admin` (set by `httpd` after it checked the password). | 403 |
| 2 | The client address must be in the local-network ranges above. | 403 |
| 3 | An endpoint that changes something refuses a cross-site request. `Sec-Fetch-Site`, if sent, must be `same-origin` or `none`. `Origin` and `Referer`, if sent, must name the same host and port as the `Host` header. A client that sends none of these headers (a script, a home-automation system) passes. | 403 |
| 4 | The method must be `GET`, `HEAD` or `POST`. | 405, with `Allow: GET, HEAD, POST` |
| 5 | The query string must be 4096 bytes or less. | 414 |
| 6 | A `POST` body must be 4096 bytes or less, and of type `application/x-www-form-urlencoded`. | 413 for the size, 400 for a bad length; another type gets an API error |

Parameters come from the query string and from a form body, in that sequence. The API keeps the first value of each name. Names must be 1 to 32 characters of `a-z`, `0-9` and `_`; other names are ignored. `+` and `%XX` are decoded. A value with a control character or a broken `%` escape is refused by the endpoint that reads it. The API never runs a parameter as a command.

`HEAD` gets the headers only. Every answer has these headers, and never a cross-origin header:

```
Content-Type: text/plain
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

Pictures are the exception for `Content-Type`: `image/jpeg` or `image/gif`.

### Answers and errors

The answers are JSON, sent as `text/plain`. All values are JSON strings, except where the tables say otherwise. Each answer has `"return"`:

| Kind | HTTP status | Body |
|---|---|---|
| Success | 200 | `{"return":"0", ...}` |
| API error (a bad parameter, the rabbit asleep, a daemon refused) | 200 | `{"return":"1","msg":"<the reason in words>"}` |
| Policy refusal (the checks above) | 400, 403, 405, 413 or 414 | `{"return":"1","msg":"<the reason>"}` |
| No login or a wrong password | 401 | the page of `httpd` |
| Unknown snapshot (`snapshot_get`) | 404 | `{"return":"1","msg":"no stored snapshot has that name"}` |

Some example error messages:

| Message | Cause |
|---|---|
| `the rabbit is asleep: wake it up first` | The endpoint drives the hardware and the rabbit sleeps. |
| `the rabbit is busy: an application is running on it` | The controller is in state 21 (interactive) or 22 (leaving interactive mode). |
| `the rabbit is busy with another request: try again` | The API's lock did not become free in 5 s. |
| `<daemon> isn't running` | The daemon is not on the D-Bus system bus. |
| `<daemon> is busy: it didn't answer in time` | The daemon did not answer in 5 s. |
| `parameter <name> must be ...` | A parameter has a bad value. |

Every endpoint that changes something writes one line to the system log with the tag `lapinlibre-web`: the endpoint, the client address and the result. The log never contains a parameter's value.

### Rules for the hardware endpoints

An endpoint that drives a daemon calls `web_hardware`:

1. It asks the controller for its state (`com.mindscape.karotz.KarotzInterface.get_status` on `com.mindscape.karotz.Controller`). In state 21 or 22 (an application owns the rabbit), it refuses.
2. It refuses while the rabbit sleeps, except for `snapshot`, `snapshot_view` and `rfid_start_record`. The rabbit sleeps when the API's sleep flag is set, or when the controller is in state 24 (asleep).
3. It takes the API's lock, `/var/lock/lapinlibre-web.lock`, and waits 5 s at most.
4. It makes a request id, `webapi:` and 16 hexadecimal digits, and gives it to each D-Bus call.

The D-Bus calls go to `com.mindscape.karotz.<Name>` at `/com/mindscape/karotz/<Name>`, interface `com.mindscape.karotz.KarotzInterface`, with `dbus-send --system`, and wait 5 s for the answer. See [system.md](system.md) for the daemons and their methods.

| Bus name | Daemon | Methods the API calls |
|---|---|---|
| `com.mindscape.karotz.Led` | `led-daemon` | `light`, `pulse` |
| `com.mindscape.karotz.Ears` | `ears-daemon` | `reset`, `move` (and the `reset_finished` signal) |
| `com.mindscape.karotz.Multimedia` | `multimedia-daemon` | `play`, `stop`, `pause`, `resume`, `next`, `previous` |
| `com.mindscape.karotz.Voice` | `voice-daemon` | `tts_speak`, `tts_stop` |
| `com.mindscape.karotz.Webcam` | `webcam-daemon` | `takeSnapshot` (and the `event` signal) |
| `com.mindscape.karotz.Controller` | `controller` | `get_status` |

### The API's state

The API keeps a small state. Most of it is in RAM, so a restart clears it.

| File | Where | Contents |
|---|---|---|
| `/var/run/lapinlibre-web/led` | RAM | The LED that the API set last: colour, pulse (0 or 1), second colour, speed. `status` shows it and the API sets it again after its own short uses of the LED. Without the file: `00FF00`, fixed. |
| `/var/run/lapinlibre-web/sleep` | RAM | The time (in seconds) when `sleep` put the rabbit to sleep. |
| `/var/run/lapinlibre-web/playback` | RAM | `playing` or `paused`, for the pause toggle of `sound_control`. |
| `/var/run/lapinlibre-web/learning` | RAM | The time when tag learning started. |
| `/var/run/lapinlibre-web/snapshots/` | RAM | The stored pictures and thumbnails, the newest 10. |
| `/var/run/lapinlibre-web/ssh_keys` | RAM | The fingerprints that `lapinlibre/ssh` shows, made again when a key file changes. |
| `/usr/etc/conf/web/ears_disabled` | flash | Present when `ears_mode` turned the API's ear moves off. |
| `/usr/etc/conf/web/tags` | flash | The tag store (mode 0600, in a 0700 folder). |

### The web page

`index.html`, `app.js` and `style.css` make the page. The page uses no external file, so it works on a network without Internet. `app.js` calls the API under `cgi-bin/` with the same origin and the browser's login. It sends reads as `GET` and actions as `POST`. It puts every value into the page as text, never as HTML.

The page loads `status` first, and again every 30 s while the page is visible, and 10 s after an action. Each section loads its own list when it comes into view.

| Section | What it does | Endpoints |
|---|---|---|
| Status | Shows the rabbit, the network, the system, the servers and the daemons. Warns when telnet is on. | `status` |
| Sleep | Go to sleep, wake up, wake up silently. | `sleep`, `wakeup` |
| LED | Colour swatches, colour, pulse, second colour, period; set or turn off. Shows the LED that the API set last. | `leds` |
| Ears | Move to two positions (0 to 16), with or without recalibration; recalibrate; random; turn the API's ear moves off or on. | `ears`, `ears_reset`, `ears_random`, `ears_mode` |
| Sound | The list of sounds with a play button; play a URL; pause or resume, stop, previous, next. | `sound_list`, `sound`, `sound_control` |
| Speech | Speak a text with a voice from the list; say the time. | `voice_list`, `tts`, `apps/clock` |
| Camera | Take a picture; show a live picture that is not stored; the gallery; delete one picture or all of them. | `snapshot`, `snapshot_view`, `snapshot_list`, `snapshot_get`, `lapinlibre/snapshot_delete`, `clear_snapshots` |
| RFID tags | Learn tags with a countdown; the tag list; edit a tag (name, colour, action); test a URL; remove the action; forget a tag or all tags. | `rfid_start_record`, `rfid_stop_record`, `rfid_list_ext`, `rfid_rename`, `rfid_assign_karotz`, `rfid_assign_url`, `rfid_test_url`, `rfid_unassign`, `rfid_delete`, `clear_rfid` |
| Radio, moods and stories | The lists from the USB key, each with a play button; a random mood. Tells how to make the files when a list is empty. | `radios_list`, `moods_list`, `stories_list`, `sound`, `apps/moods`, `stories` |
| Logs | Show the system, kernel or `mpd` log (1 to 1000 lines); save it as a file. | `lapinlibre/logs` |
| SSH | Shows if SSH logins are possible, the commands to connect, the installed keys and the host key fingerprints. | `lapinlibre/ssh` |
| Restart | Restart the rabbit after a confirmation, then wait until it answers again (5 minutes at most). | `reboot`, `lapinlibre/status` |

After each action, the section shows "The URL of this action": the `GET` URL that does the same action. The URL never contains a password; the page shows `<password>` in its place.

### Endpoints

All endpoints are under `/cgi-bin/`. "Read" endpoints change nothing. "Change" endpoints follow the cross-site rule. "Hardware" endpoints also follow the rules for the hardware endpoints. In the parameter column, the default value is in parentheses.

#### LED

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `leds` (change, hardware) | `color` RRGGBB, `#` allowed (the API's last colour, else `00FF00`; `000000` is off); `pulse` 0 or 1 (0); `color2` RRGGBB (`000000`); `speed` 25 to 10000 ms (700); `nomemory` 0 or 1 (0) | Sets the LED fixed (`Led.light`) or pulsing between `color` and `color2` without end (`Led.pulse`). Without `nomemory=1`, keeps the setting as the API's LED state. `blink` is ignored; with `pulse=1`, a `blink` other than 0 is refused. Before the first setting after a start, it stops the start-up LED program `led` if it still runs. | `color`, `secondary_color`, `pulse`, `no_memory`, `speed`, `return` |

#### Ears

One turn of an ear is 17 steps. The API moves the ears at the speed 100.

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `ears` (change, hardware) | `left`, `right` 0 to 255 (required); `noreset` 0 or 1 (0) | Recalibrates the ears and waits for `reset_finished` (10 s at most), unless `noreset=1`. Then moves each ear to its position modulo 17 (`Ears.move`). Answers when `ears-daemon` accepts the move. | `left`, `right` (as given), `return` |
| `ears_random` (change, hardware) | `noreset` 0 or 1 (0) | As `ears`, to two random positions from 0 to 16. | `left`, `right` (the positions chosen), `return` |
| `ears_reset` (change, hardware) | none | Recalibrates both ears (`Ears.reset`). Does not wait for the end. | `return` |
| `ears_mode` (change) | `disable` 1 turns the moves off; any other value, or none, turns them on | Turns the API's own ear moves off or on: `ears`, `ears_reset`, `ears_random`, and the ears of `sleep`, `wakeup` and `apps/moods`. The controller still moves the ears. Writes the flash only when the setting changes. Turning them on also recalibrates the ears, unless the rabbit sleeps or an application owns it. | `return`, `disabled` |

While the moves are off, `ears`, `ears_random` and `ears_reset` answer with the error `the API's ear moves are disabled (ears_mode)`.

#### Sound and music

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `sound` (change, hardware) | `id` a sound's name, or `url` an `http://` or `https://` URL (one of the two) | Stops the playback (`Multimedia.stop`), then plays the file or the URL (a file, a stream or a playlist) (`Multimedia.play`). Answers when the playback starts. | `return`; with `url`, also `url` |
| `sound_list` (read) | none | Lists the sounds `sound` plays: the `.mp3` and `.wav` files of `/mnt/usbkey/sounds`, then of `/usr/karotz/sounds`; each name once, sorted. | `sounds`: a list of `{"id": name}` |
| `sound_control` (change, hardware) | `cmd`: `pause`, `resume`, `stop`, `quit`, `next` or `previous` (required) | Controls the playback of `multimedia-daemon`. `pause` pauses, or resumes what the API paused last (a toggle). `quit` is the same as `stop`. | `return`, `cmd` |
| `radios_list` (read) | none | Lists the stations in `radios.txt` on the USB key: one `name|url` for each line; blank lines, `#` lines and lines with a bad URL are skipped; 500 stations at most. `sound?url=` plays a station. Empty without a USB key. | `streams`: a list of `{"id": number, "name", "url"}`, numbered from 1 |

A sound's name has 1 to 64 characters of `A-Z`, `a-z`, `0-9`, `_` and `-`. The USB key comes first; for each folder, `.mp3` comes before `.wav`. [sound-map.md](sound-map.md) lists the rabbit's own sounds. A URL has 1024 bytes at most, a host name or IPv4 address, an optional port, and only URL characters and `%XX` escapes.

#### Speech

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `tts` (change, hardware) | `text` 1 to 1000 bytes of UTF-8 (required); `voice` a voice number from 1 to 88, a language code (`en`, `en-GB`) or a voice name; `mute` 0 or 1 (0); `nocache` accepted and ignored | Says the text through `voice-daemon` (`Voice.tts_speak`) and the speech server that `voice.conf` gives for the language. Removes one pair of double quotes around the text, and all `<` and `>`. Tabs and line ends become spaces. Answers when `voice-daemon` accepts the text, before the speech ends. | `return`, `id` (an MD5 of language, gender and text), `played` (JSON `true` or `false`), `cache` (always `false`), `voicelanguage`, `voicegender` |
| `voice_list` (read) | none | Lists the voice numbers whose language has a speech server in `/usr/etc/conf/voice.conf`. | `voices`: a list of `{"id", "lang"}`, for example `{"id":"4","lang":"English (UK), female"}` |
| `apps/clock` (change, hardware) | `hour` 0 to 23 (none: now) | Says the hour given, on the hour, or the time now, in the rabbit's language. English, French, German and Spanish have their own sentence. Other languages get the English sentence, through an English speech server if there is one. | `return`, `hour` |
| `display_cache` (read) | none | There is no speech cache, so the cache is always empty. | `{"count":"0","return":"0"}` |
| `clear_cache` (change) | `id` accepted and ignored | There is no speech cache to clear. | `return`, `msg` |

The voice numbers come in pairs for each language, in this sequence: `fr fr-CA en-US en-GB de it es nl af sq ar hy bs pt-BR hr cs da en-AU eo fi el ht hi hu is id ja ko la no pl pt-PT ro ru sr sh sk sw sv ta th tr vi cy`. The odd number of a pair is male and the even number is female. The speech server has one voice for each language, so the two numbers of a language sound the same. A voice name, no voice, or a language without a speech server gives the rabbit's language (`lang` in `/usr/etc/conf/karotz.conf`, else `en-US`). Without a speech server for the language, `tts` answers `no speech server is set for <lang>: set SERVER= (or TTS_SERVER=) in lapinlibre.txt`.

#### RFID tags

A tag's id is 16 hexadecimal digits. The API stores it in upper case.

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `rfid_start_record` (change, hardware, also while asleep) | none | Starts tag learning for 2 minutes. Stops the playback. The LED pulses orange (`FF8800`, 400 ms). The tone `button-long.wav` plays (not while the rabbit sleeps). | `return` |
| `rfid_stop_record` (change) | none | Stops tag learning. Sets the API's LED state again (off while the rabbit sleeps) and plays `button-click.wav` (not while it sleeps). While an application owns the rabbit, it only stops learning. | `return` |
| `rfid_list` (read) | none | Lists the learned tags. Never shows the data of an action. | `tags`: a list of `{"id", "type", "color", "cmd", "var", "name"}` |
| `rfid_list_ext` (read) | none | Lists the learned tags with the names of the codes. | `tags`: a list of `{"id", "name", "type", "type_name", "color", "color_name", "type", "cmd", "cmd_name"}` (see below) |
| `rfid_info` (read) | `tag` (required) | One learned tag. | `id`, `type`, `type_name`, `color`, `color_name`, `cmd`, `cmd_name`, `var`, `name`, `return` |
| `rfid_rename` (change) | `tag` (required); `name` 0 to 64 characters (none or empty: no name); `color` 0 to 99 (none: unchanged) | Sets the tag's name and, optionally, its colour code. | `return` |
| `rfid_assign_karotz` (change) | `tag` (required); `url` base64 of `/cgi-bin/<endpoint>?<query>` (required); `name` (none: unchanged) | Gives the tag one of the API's endpoints as its action, in place of any other action. The URL can also start with `http://` and this rabbit's address: `127.0.0.1`, `localhost`, its name, `<name>.local` or its IP address, with any port. The endpoint must be one in the list below, and the query must pass the endpoint's own checks now (the endpoint runs in check mode and does nothing). | `return` |
| `rfid_assign_url` (change) | `tag` (required); `url` an `http://` or `https://` URL (required); `param` base64 of a query string to add to the URL; `user` and `password` for basic authentication (both or neither, 1 to 64 printable characters, no `:` in `user`; `user` with an empty `password` keeps the password the tag's URL action stores for that user); `name` (none: unchanged) | Gives the tag a URL request as its action, in place of any other action. The URL and the login are stored; the password is never shown again. | `return` |
| `rfid_test_url` (change) | `url`, `param`, `user`, `password` as for `rfid_assign_url`; `tag` (optional): with it, `user` with an empty `password` uses the password that tag stores, as for `rfid_assign_url`; `name` accepted and ignored | Sends the URL request now, as the tag listener sends it. | `return`, `msg`: the first 1024 bytes of the answer's body, as text; or an error with the reason |
| `rfid_unassign` (change) | `tag` (required) | Removes the tag's action and name. The tag stays known. | `return`, `tag` |
| `rfid_delete` (change) | `tag` (required) | Removes the tag and its action. | `return`, `tag` |
| `clear_rfid` (change) | none | Removes all learned tags and their actions. | `return` |
| `tag_list` (read) | none | Always an error that tells you to use `rfid_list`. | `return` 1, `msg` |

An unknown tag gives the error `no tag <id> is known: learn it first (rfid_start_record)`.

The endpoints that a tag can run (`WEB_TAG_ENDPOINTS` in `web.sh`) are: `tts`, `sound`, `sound_control`, `leds`, `ears`, `ears_reset`, `ears_random`, `sleep`, `wakeup`, `snapshot`, `apps/clock`, `apps/moods` and `stories`.

The codes and their names:

| Field | Codes |
|---|---|
| `type` | 1 `FLATANOZ`, 2 `NANOZTAG`, 3 `ZSTAMPS`, 15 `KEYRING`, other `UNKNOWN` |
| `color` | 1 `RED`, 2 `BLUE`, 3 `GREEN`, 4 `YELLOW`, 5 `PINK`, 6 `BLACK`, 7 `GREY`, 8 `ORANGE`, 9 `PURPLE`, 10 `WHITE`, 51 `BROWN`, other `UNKNOWN` |
| `cmd` | 0 no action; 3 `URL` (a URL request); 5 `KAROTZ` (one of the API's endpoints). The names 1 `VERA`, 2 `EEDOMUS` and 4 `ZIBASE` exist for the answers only; the store keeps only 0, 3 and 5. |
| `var` | Always -1. |

#### The tag store and the tag listener

The tag store is `/usr/etc/conf/web/tags`. It has one line for each tag, with the fields separated by tabs:

```
id  type  color  cmd  var  name  action1  action2  action3
```

For `cmd` 3, the three action fields are the URL, the user and the password. For `cmd` 5, they are the endpoint and its query string. Lines that start with `#` are comments. Each change reads the store, applies the change and writes the full file again, under the lock `/var/lock/lapinlibre-tags.lock`. Each read checks every field again: a line with a bad or repeated id is skipped, and an action that fails its rules becomes "no action".

The tag listener, `/usr/scripts/tag_listener`, does the tags' work. `immortaldog` keeps it running (`/usr/etc/init.d/S40lapinlibre`). It listens only for the `tag_in` signal of `rfid-daemon` (`/com/mindscape/karotz/Rfid`). For each tag on the nose:

- While learning is on, it adds a new tag to the store with the type and colour codes from `rfid-daemon`. The LED flashes green and `notify.mp3` plays. A known tag gets a yellow flash and `button-triple.wav`. Then the orange pulse comes back.
- Outside learning, for a stored tag with an action, the LED flashes green (not while the rabbit sleeps or an application owns it). Then the listener does the action and sets the API's LED state again.
- A URL request is an HTTP `GET` with `curl`: 15 s at most, 3 redirects at most, `http` and `https` only, certificates checked, the answer's body discarded. The log shows only the host and the result.
- An endpoint action runs the endpoint's program on the rabbit with the stored query, as user `admin` from `127.0.0.1`, for 30 s at most. The endpoint applies all its own checks, also the sleep rule.
- It does one action at a time. It ignores a tag that comes while an action runs.

The controller also reads the store (`web_api_has_tag` in `userland/src/usr/controller/dbus_listeners.cc`). When no application owns a tag but the store has it, the controller does not play the "tag unknown" sound, so the tag listener acts on the tag alone.

#### Snapshots

`webcam-daemon` flashes the LED and plays `shutter.mp3` for every picture. The API cannot stop this. After the picture, the API sets its LED state again (off while the rabbit sleeps). All snapshot endpoints work while the rabbit sleeps.

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `snapshot` (change, hardware, also while asleep) | `silent` accepted; it cannot make the camera silent | Takes a picture (`Webcam.takeSnapshot`, 10 s at most). Stores it in RAM as `snapshot_<YYYY_MM_DD_HH_MM_SS>.jpg`, with a quarter-size GIF thumbnail `snapshot_<time>.thumb.gif` when `djpeg` can make one. Keeps the newest 10. A restart deletes them. | `filename`, `thumb` (empty without a thumbnail), `return` |
| `snapshot_view` (change, hardware, also while asleep) | `silent` as above | Takes a picture and sends the JPEG itself. Does not store it. | The JPEG (`image/jpeg`); an error as JSON text |
| `snapshot_list` (read) | none | Lists the stored pictures, oldest first. The thumbnail's name is the picture's name with `.thumb.gif` in place of `.jpg`. | `snapshots`: a list of `{"id": file name}` |
| `snapshot_get` (read) | `filename`: a name that `snapshot` gave, picture or thumbnail | Sends the stored file (`Cache-Control: private, no-store`). | The JPEG or GIF; HTTP 404 for any other name |
| `clear_snapshots` (change) | none | Deletes all stored pictures and thumbnails. | `return` |

#### Moods, stories and the clock

Moods and stories are MP3 files on the USB key, in the folders `moods/` and `stories/`. A name has 1 to 64 characters of letters, digits, `_` and `-`. The optional files `moods/moods.txt` and `stories/stories.txt` give labels, one `name:label` line for each file. The lists put names that are numbers first, in their numeric sequence, then the other names. `apps/clock` is in the Speech table.

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `apps/moods` (change, hardware) | `id` a mood's name (none: a random mood); `lang` accepted and ignored | Recalibrates the ears (unless the API's ear moves are off), stops the playback, then plays the mood. Answers when the playback starts. | `moods` (the mood played), `return` |
| `moods_list` (read) | none | Lists the moods. Empty without a USB key. | `moods`: a list of `{"id", "text"}` |
| `stories` (change, hardware) | `id` a story's name (required) | Stops the playback, then plays the story. | `return` |
| `stories_list` (read) | none | Lists the stories. Empty without a USB key. | `stories`: a list of `{"id", "name"}` |

#### Sleep and wake-up

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `sleep` (change) | none | Stops the playback and the speech, recalibrates the ears and moves them down to position 4 (unless the API's ear moves are off), turns the LED off, and sets the API's sleep flag in RAM with the time. It does not ask the controller to sleep. A restart wakes the rabbit. Refused when the rabbit already sleeps. | `return` |
| `wakeup` (change) | `silent` 0 or 1 (0) | Clears the API's sleep flag (also when the rabbit is awake). Sets the LED to a green pulse (`00FF00`, 700 ms) as the API's LED state, recalibrates the ears (unless the moves are off), and plays `wakeup.mp3` unless `silent=1`. A controller that sleeps by its own schedule stays asleep. | `return`, `silent` |

While the rabbit sleeps, the hardware endpoints refuse, except the snapshot endpoints and `rfid_start_record`.

#### System and status

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `status` (read) | `format`: `json` or `xml` (`json`) | The rabbit's state, with LapinLibre's own status in `lapinlibre`. | See below |
| `get_version` (read) | none | The installed build and the root file system's version (`/karotz/etc/rootfs_version`). | `version`, `rootfs_version` (`Unknown` without the file), `patch_version` (always `N/A`), `return` |
| `get_free_space` (read) | none | The used percentage of the `/usr` flash partition and of the USB key. | `karotz_percent_used_space`, `usb_percent_used_space` (`-1` without a USB key), `return` |
| `setclock` (change) | none | Sets the clock one time from `pool.ntp.org` (`busybox ntpd -q`), 28 s at most. | `return` |
| `reboot` (change) | none | Answers, then the rabbit restarts after about 10 s. | `return` |
| `tools_log` (read) | none | The newest 200 lines of `/var/log/messages`. | Plain text |
| `tools_ps` (read) | none | The process list (`ps`). | Plain text |

The JSON fields of `status`:

| Field | Value |
|---|---|
| `version` | The build id and the build version, as `<build id> (<build>)`. |
| `ears_disabled` | `1` when the API's ear moves are off. |
| `sleep`, `sleep_time` | `1` when the rabbit sleeps (the API's flag or the controller's state 24); the time in seconds when `sleep` started it, else `0`. |
| `led_color`, `led_pulse` | The API's LED state. |
| `tts_cache_size` | Always `0`. |
| `usb_free_space`, `karotz_free_space` | The free space as `df -h` shows it (for example `63.8M`); `-1` without a USB key. |
| `eth_mac` | Always `00:00:00:00:00:00`. |
| `wlan_mac` | The MAC address of `wlan0`. |
| `nb_tags`, `nb_moods`, `nb_sounds`, `nb_stories` | The numbers of tags, moods, sounds and stories. |
| `karotz_percent_used_space`, `usb_percent_used_space` | The used percentages (empty without a USB key). |
| `data_dir` | `/usr/etc/conf/web`. |
| `return` | `0`. |
| `lapinlibre` | An object: the same fields as `lapinlibre/status`, without `return`. |

With `format=xml`, the answer is `<?xml ...?><OPENKAROTZ>` with the elements `VERSION`, `EARS_DISABLED`, `SLEEP`, `SLEEP_TIME`, `LED_COLOR`, `LED_PULSE`, `TTS_CACHE_SIZE`, `ETH_MAC`, `WLAN_MAC`, `KAROTZ_USED_SPACE`, `USB_USED_SPACE` and `RETURN`.

#### LapinLibre's own endpoints

| Endpoint | Parameters | What it does | Answer |
|---|---|---|---|
| `lapinlibre/status` (read) | none | LapinLibre's own status for the web page. | See below |
| `lapinlibre/logs` (read) | `lines` 1 to 1000 (200); `source`: `system`, `kernel` or `mpd` (`system`) | The newest lines of a log, oldest first. `system` is `/var/log/messages.0` and `/var/log/messages`; `kernel` is `dmesg`; `mpd` is `/var/log/mpd.log`. | Plain text; an error as JSON |
| `lapinlibre/tag_action` (read) | `tag` (required) | A learned tag's action, for the page's tag editor. Never sends a stored password. | `return`, `tag`, `cmd` (`0` none, `3` a URL request, `5` one of the API's endpoints); for `5`: `endpoint` and `query`; for `3`: `url`, `user` and `password_set` (`1` when a password is stored) |
| `lapinlibre/snapshot_delete` (change) | `filename` (required): a picture's name as `snapshot` gave it (`snapshot_<time>.jpg`) | Deletes that stored picture and its thumbnail. Any other name, or one not stored, is refused. | `return`, `filename` |
| `lapinlibre/ssh` (read) | none | What the SSH section of the page shows. Never sends a key itself. | `return`, `enabled` (`1` when `/usr/karotz/.ssh/authorized_keys` has a key), `keys` (a list of `{"type", "comment", "fingerprint"}`), `host_keys` (a list of `{"type", "fingerprint"}` from `/usr/etc/conf/dropbear`), `hostname`, `ip`, `users` (`["root","karotz"]`), `telnet` |

The fingerprints are SHA-256, in the form `SHA256:<base64>`.

The fields of `lapinlibre/status`:

| Field | Value |
|---|---|
| `build`, `build_id` | The build version (`/karotz/etc/lapinlibre-build`) and the installed build's id (`/usr/etc/conf/lapinlibre_build_id`). |
| `hostname`, `ip`, `netmask`, `gateway`, `dns`, `mac` | The network of `wlan0`. |
| `address_mode` | `dhcp` or `fixed` (`IP=` set in `/usr/etc/conf/network.conf`). |
| `wifi_ssid`, `wifi_signal` | The Wi-Fi network's name and the link quality. |
| `time`, `timezone` | The local time (ISO 8601 with the offset) and `/etc/TZ`. |
| `uptime` | Seconds since the start (a JSON number). |
| `load` | The three load averages. |
| `mem_total_kb`, `mem_free_kb` | Memory; free is MemFree, Buffers and Cached together (JSON numbers). |
| `usr_free_kb`, `usr_used_percent` | The `/usr` flash partition (JSON numbers). |
| `usb_mounted`, `usb_free_kb`, `usb_used_percent` | `1` or `0`; the USB key's space when it is mounted (JSON numbers). |
| `language`, `app_server` | `lang` and `ip_server` in `/usr/etc/conf/karotz.conf`. |
| `speech_server` | The speech server for the language, from `/usr/etc/conf/voice.conf`. |
| `learning_left` | The seconds of tag learning left, `0` when learning is off (a JSON number). |
| `controller` | The controller's state in words: `connecting`, `connected`, `available`, `updating`, `updating configuration`, `interactive`, `leaving interactive mode`, `disconnected`, `asleep`, `waking up`, `state <n>` or `not running`. |
| `daemons` | An object with `1` or `0` for each program: `led`, `rfid`, `webcam`, `button`, `ears`, `multimedia`, `voice`, `scheduler`, `serial`, `k2k` (each `<name>-daemon`), `controller`, `mpd`, `mdnsd`. |
| `ssh`, `telnet` | `1` when SSH logins are possible; `1` when telnet is on. |
| `web_user` | `admin` when a web password is set, else empty. |

### Compatibility

The endpoint names, their parameters and their answer shapes follow the OpenKarotz-compatible API that existing clients and home-automation plugins call. For this reason:

- All values in the answers are JSON strings (`"return":"0"`), except the fields that LapinLibre adds.
- Errors use HTTP 200 with `"return":"1"` and `"msg"`.
- The parameters that clients send but that have no effect here are accepted and ignored: `nocache` of `tts`, `silent` of `snapshot` and `snapshot_view`, `lang` of `apps/moods`, `id` of `clear_cache`.
- The fields with no source on this rabbit have fixed values: `patch_version` `N/A`, `tts_cache_size` `0`, `eth_mac` `00:00:00:00:00:00`, `cache` `false`.
- `display_cache`, `clear_cache` and `tag_list` exist so that clients get an answer.
- `status?format=xml` gives the `<OPENKAROTZ>` document.
- `rfid_list_ext` has two `type` keys in each tag. The first is the tag type code, the second is the action's name. Clients read the last key, so they see the action's name.
- `rfid_assign_karotz` takes the endpoint's URL in base64, also with `http://` and the rabbit's address.

### Tests

`tests/device/web-api.sh` tests the page and the API on the real rabbit, from RAM. It starts a second `httpd` on `127.0.0.1:8099` with copies of the new files and a test password, and never changes the installed page or its data. It checks the login (401 without it), the answer shapes, the headers, the refusals (cross-site 403, `PUT` 405, a long query 414), and the tag store with a test tag. With `hardware`, `tag` or `reboot`, it also drives the LED, ears, sound, speech, camera and sleep, the tag listener with a real tag, and the restart. The script's header gives the commands. `tests/image/test_web_api.py` tests the same programs on the build host, as `httpd` runs them, with stubs in place of the rabbit's commands. See [build.md](build.md) for the tests.
