<!-- SPDX-License-Identifier: MIT -->
# App server

The app server is the platform server for the rabbit. You run it on your own computer. The rabbit's controller connects to it, logs in, and gets its applications, schedules and commands from it. The folder `tools/app-server/` contains these programs:

| Program | What it does |
|---|---|
| `app_server.py` | The platform server. It also serves the application downloads, the HTTP endpoints for applications and the REST API. It can also start the speech server and the speech-recognition server. |
| `appctl.py` | The control tool. It sends commands to a running `app_server.py`. |
| `tts_server.py` | The speech (TTS) server. voice-daemon on the rabbit sends text to it and plays the MP3 that it returns. |
| `asr_server.py` | The speech-recognition (ASR) server. It does not recognise speech. It sends back a result that you choose. |
| `rest_api.py` | The REST API for external applications. `app_server.py` loads it. You do not run it alone. |

The app server is not part of the install image, and the build does not need it. It has no authentication (see [Security](#security)). For the platform protocol, the controller and karotzVM, see [system.md](system.md). For the USB key and `lapinlibre.txt`, see [install.md](install.md).

## How to

### Install the app server

Use this procedure one time, before you start the server for the first time.

1. Install Python 3.10 or later and `uv`.
2. Go to the folder of the app server: `cd tools/app-server`
3. Make the virtual environment: `uv sync`
4. Make the message module: `uv run --with grpcio-tools python -m grpc_tools.protoc -I ../../userland/proto --python_out=. platform-message.proto`

`uv sync` installs `protobuf`, `lameenc` and `kokoro-onnx` from PyPI into `.venv`. Install packages only from the official registries, and get approval for new dependencies before you use them. Without the packages, the servers use the `protoc` command for messages and `ffmpeg` for MP3. See [Codec and packages](#codec-and-packages).

### Get the Kokoro voice files

Use this procedure if you want the Kokoro voices for speech.

1. Go to the root folder of the repository.
2. Run `uv run image/tools/install_prompts.py`. It downloads the model files to `~/.cache/lapinlibre/kokoro` and checks their SHA-256.

### Start the server

Use this procedure to start the platform, the speech server and the speech-recognition server together.

1. Go to the folder: `cd tools/app-server`
2. Run `uv run python app_server.py --apps example-apps --settings settings.example.json --tts-port 8081 --asr-port 8443`
3. Read the start-up lines. They name the applications, the codec, the speech engines and the ports.
4. Press Ctrl-C to stop the server.

### Point the rabbit at the server

Use this procedure to make the rabbit use your server. The install does it from `lapinlibre.txt`.

1. Open `lapinlibre.txt` on the USB key.
2. Set `SERVER=` to the address of your computer, for example `SERVER=192.168.1.10`.
3. Leave `TTS_SERVER=` and `ASR_SERVER=` empty. The install then uses `SERVER` with ports 8081 and 8443.
4. Install from the USB key (see [install.md](install.md)).

See [How the rabbit finds the server](#how-the-rabbit-finds-the-server) for the files that the install changes.

### Open the firewall on Windows

Use this procedure if the rabbit cannot connect to the server on Windows.

1. Under WSL, test the connection first. The firewall can already let the connections through.
2. Add an inbound firewall rule for your private network. This needs administrator rights.
3. In the rule, allow TCP ports 9123, 8080, 8081 and 8443, or the `python.exe` of the `.venv`.

### Check the connection

Use this procedure to see if the rabbit is connected and logged in.

1. Run `uv run python appctl.py status`.
2. Look at `connected` and `logged_in`. Both must be `true`.

### Start an application

Use this procedure to start an application on the rabbit now.

1. Make sure that the settings file has `timezone`.
2. Run `uv run python appctl.py run hello`.
3. Wait about 3 seconds. The application starts on the rabbit.

### Control the rabbit by hand

Use this procedure to move the ears, light the LED or speak.

1. Run `uv run python appctl.py take`. The rabbit goes into interactive mode.
2. Run `uv run python appctl.py ears 5 12`.
3. Run `uv run python appctl.py led 00FF00`.
4. Run `uv run python appctl.py say Hello`.
5. Run `uv run python appctl.py release`. The rabbit goes back to normal.

### Use the shell

Use this procedure to type many commands and see the rabbit's messages as they arrive.

1. Run `uv run python appctl.py shell`.
2. Type a command at the `rabbit>` prompt, for example `take`.
3. Type `help` for the list of commands.
4. Type `quit` to stop the shell.

### Send new settings to the rabbit

Use this procedure after you change the settings file or an application.

1. Change the settings file or the application folder.
2. If you changed an application, increase `<version>` in its `descriptor.xml`. The rabbit then downloads it again.
3. Run `uv run python appctl.py reload`.

### Run the device test

Use this procedure to test the rabbit's functions one step at a time.

1. Put the address of your computer in the `server` parameter of the `devicetest` instance in the settings file, for example `"server": "http://192.168.1.10:8080"`.
2. Start the server with `--tts-port 8081 --asr-port 8443`.
3. Run `uv run python appctl.py run devicetest`.
4. Read the results on the console of the server, or run `uv run python appctl.py app-log`.
5. Push the rabbit's button when the test asks for it (the last step, 15 seconds).

### Run the tests of the app server

Use this procedure to test the app server against a fake controller.

1. Go to the root folder of the repository.
2. Run `python3 -I tests/app_server/test_app_server.py`. To use a specific protoc, add its path as the argument.

`tests/run.sh` also runs these tests, with the protoc 2.3.0 of the system build. See [build.md](build.md).

## Technical details

This section is the reference: how each part works, with its options, files and protocols.

### What the server does

This list shows what `app_server.py` does after the controller connects.

- It accepts one controller connection. A new connection replaces the old one.
- It sends `authenticationToken` with a random token. It accepts the `authenticationResponse` for any id and password. The rabbit's `account.conf` must contain both an id and a password, or the controller does not answer the login.
- After the login, it sends the configuration: `config`, then `scheduleConfig`, then `contactList` (if the settings have contacts), then `pendingmessage`. Without `schedules` in the settings, the controller answers the `scheduleConfig` with `response { code: ERROR }`: it answers `OK` only when the message adds at least one application schedule. The clock and the time zone are set either way.
- It answers each `ping` from the controller with `response { code: OK }`, and does not log it.
- It answers each `karotzMessageQuery` (`NEW`, `LIST`, `GET`, `DELETE`, `READ`) from its message store with a `karotzMessageResponse`. After `NEW`, `DELETE` and `READ`, it sends `pendingmessage` again.
- It logs all other messages from the controller (button, RFID, ears, events, replies) and keeps the last 200 for `appctl.py events`. The log marks what it sends with `->` and what it receives with `<-`. Each sent message shows its `id`, and the controller's reply carries the same value as its `correlationId`.
- It sends the commands that `appctl.py` and the REST API ask for, and waits for the replies (by `correlationId`).

Messages are `PlatformMessage` protobufs from `userland/proto/platform-message.proto`. Each message on the TCP connection has a 4-byte length in network byte order before it. See [system.md](system.md) for the protocol.

### Options of app_server.py

| Option | Default | What it does |
|---|---|---|
| `--apps DIR` | (required) | The applications folder. |
| `--settings FILE` | none | The settings file (JSON). Without it, each application has one instance, `default`, and there are no schedules. |
| `--listen ADDR` | `0.0.0.0` | The address for the platform port, the HTTP port, the speech server and the speech-recognition server. |
| `--port N` | `9123` | The platform port (`port_server` on the rabbit). |
| `--http-port N` | `8080` | The HTTP port: downloads, application endpoints and REST API (`url_app` on the rabbit). |
| `--control-port N` | `9124` | The control port for `appctl.py`. It listens on `127.0.0.1` only. |
| `--control-socket PATH` | none | A Unix socket for control, instead of the control port. |
| `--hold` | off | Do not log the controller in until `appctl.py login`. The tests use it. |
| `--tts-port N` | none | Also start the speech server on this port. If there is no MP3 encoder or no speech engine, the server logs a message and continues without speech. |
| `--asr-port N` | none | Also start the speech-recognition server on this port. |
| `--asr-answer A` | `first` | What the speech-recognition server answers: `first`, `nomatch`, or the words. |
| `--uploads DIR` | `uploads/` in `tools/app-server` | The folder for files posted to `/upload`, and for `asr-last.raw`. The server makes it if it does not exist. |
| `--codec C` | `auto` | `auto`, `python` or `protoc`. See [Codec and packages](#codec-and-packages). |
| `--protoc CMD` | `protoc` | The protoc command, for the `protoc` codec. |
| `--proto PATH` | `../../userland/proto/platform-message.proto` (from the script) | The protocol definition, for the `protoc` codec. |

When the speech server starts from `app_server.py`, it uses the engine `auto` and the default Kokoro model folder.

### Codec and packages

The codec changes messages between protobuf bytes and protobuf text format.

| `--codec` | What it uses |
|---|---|
| `auto` | The `protobuf` package and `platform_message_pb2.py` if both are available, otherwise the `protoc` command. |
| `python` | The `protobuf` package. The server stops if it is not available. |
| `protoc` | The `protoc` command, with `--encode` and `--decode`. |

The start-up line says `messages through the protobuf package` or `messages through the protoc (...)`. `platform_message_pb2.py` is generated code and git ignores it. `pyproject.toml` requires Python 3.10 or later. All three packages are optional at run time.

### The settings file

The settings file is a JSON object. `settings.example.json` is an example. All keys are optional.

| Key | Type | What it does |
|---|---|---|
| `lang` | string | The rabbit's language, for example `en-US`. It goes into `config` and into the launch grammar. The grammar uses `en-US` if `lang` is not set. |
| `timezone` | string | The rabbit's time zone, by a name from the rabbit's `/etc/timezone.list`, for example `Europe/Paris`. With it, each `scheduleConfig` also sets the rabbit's clock and time zone. `run` needs it. |
| `playsounds` | boolean | Goes into `config`. Default `true`. |
| `apps` | object | Per application (by its folder name): `instances` and `secret`. |
| `apps.KEY.instances` | list | The instances of the application. Default: one instance named `default`. |
| `apps.KEY.secret` | string | The secret for the REST API signature check. Without it, the server does not check the signature. |
| `schedules` | list | Schedules for applications. |
| `wakeup` | list | Wake-up and sleep times per day. |
| `rabbit` | object | The contact that a new message (`NEW`) is from. Default: `{"firstname": "my", "lastname": "rabbit", "uuid": "rabbit"}`. |
| `contacts` | list | The contact list that the server sends after the login. `karotz.contact.get` returns it. |
| `messages` | list | The messages in the message store at the start. |
| `social` | object | The social keys in `config`, for `social.init`. |

An instance has these keys:

| Key | What it does |
|---|---|
| `name` | The instance name (required). |
| `uuid` | The instance id. Default: a UUID made from the application key and the instance name, which does not change. |
| `rfid` | The UID of an RFID tag, 16 hexadecimal digits. The server changes it to upper case. When this tag touches the rabbit's nose, the controller starts this instance. |
| `params` | An object of string parameters. The application reads them as `params[instanceName]`. |
| `voice` | The words that start this instance by speech recognition. Default: the application's `<name>` for the `default` instance, otherwise the name and the instance name. |

A schedule has `app`, `instance` (default `default`) and one of these:

| Key | Example | What it does |
|---|---|---|
| `weekly` | `{"day": 1, "hour": 8, "minute": 0}` | Every week on this day and time. `day` is 0 (Sunday) to 6 (Saturday); 7 is also Sunday. |
| `every` | `600` | Every N seconds. |

A schedule without `weekly` or `every` is ignored.

A `wakeup` entry is `{"day": D, "wake": [HOUR, MINUTE], "sleep": [HOUR, MINUTE]}`. `day` is 1 (Monday) to 6 (Saturday), and 0 or 7 for Sunday.

A contact has `firstname`, `lastname`, `alias`, `uuid` and `objects` (a list of `{"idDB", "name"}`). A message has `id`, `from` (a contact), `to` (a contact, default `{"uuid": "rabbit"}`), `date` (seconds since the epoch), `status` (`READ` or `UNREAD`, default `UNREAD`), `type` (`TEXT`, `SOUND`, `XML` or `MIXED`, default `TEXT`), `header` and `content`. While a message is `UNREAD`, the server sets the pending-message flag, and the LED pulses green and blue.

The `social` keys are `twitterConsumerKey`, `twitterConsumerSecret`, `twitterToken`, `twitterTokenSecret`, `facebookToken` and `soundcloudToken`.

The server reads the settings file at the start and on `appctl.py reload`. `reload` also makes the message store again from the file, so messages added since the start are lost.

### Schedules and time zones

Each `scheduleConfig` replaces all the schedules on the rabbit and applies the wake-up and sleep times again. On a day with a wake-up time, this can reset the rabbit if no application is in interactive mode. Thus the server sends `scheduleConfig` only after the login, on `reload` and on `run`.

The rabbit's `/etc/timezone.list` is old. Some zones on the rabbit use old daylight-saving rules, and some new zone names (for example `Europe/Kyiv`) are missing. An unknown name gives Central European time. For these zones, use a listed zone with the same offset and the same daylight-saving rules.

### The applications folder

The applications folder has one subfolder for each application. The name of the subfolder is the application's API key. A subfolder without `descriptor.xml` is not an application. A `descriptor.xml` that cannot be read is skipped, and the server logs it.

| Field in `descriptor.xml` | Default | What it does |
|---|---|---|
| `<version>` | `1` | The version. A new value makes the rabbit download the application again. |
| `<name>` | the folder name | The name. The launch grammar uses it. |
| `<accesses>` with `<access>` items | none | What the application can use, for example `led`, `ears`, `tts`, `multimedia`, `webcam`, `button`, `rfid`, `http`, `asr`, `message`, `contact`, `twitter`, `facebook`. |
| `<deployment>` | `hosted` | `hosted`: the rabbit runs `main.js`. `external`: the application runs on its own server and uses the REST API. |
| `<callback>` | none | For an `external` application: the URL that gets the events as XML. |

`main.js` is the application's JavaScript, which karotzVM runs. It calls `karotz.connect("127.0.0.1", 9123)` and `karotz.start(...)`. This connection goes to the controller on the rabbit, not to the app server. The variables `instanceName` and `params` give the instance and its parameters. See [system.md](system.md) for the JavaScript API.

The two example applications:

| Application | What it does |
|---|---|
| `hello` | Lights the LED green, moves the ears to 5 and 5, says "Hello from" and the instance name, then exits. |
| `devicetest` | Tests the LED, ears, speech, a sound, `http.get`, messages, contacts, social keys, `karotz.info`, a photo and a recording uploaded to the server, speech recognition, the USB key's artist list, ping and the button. It sends each result to `<server>/log`. A step without an answer shows as "timed out". |

### How an application gets to the rabbit and starts

The server sends one `configapp` for each application in `config`, with the API key, version, name, deployment, accesses and instances. The controller downloads a hosted application as a zip from `<url_app>/file/<apiKey>/<version>/get`. The server makes the zip from the application's folder when the request arrives, without `config.bin`.

The platform has no message that starts an application. The controller starts a hosted application only from a trigger:

| Trigger | How it works |
|---|---|
| RFID tag | The instance's `rfid` is in `config`. The controller starts the instance when that tag touches the nose. |
| Speech | The controller's launch grammar has one alternative for each instance of each hosted application, with the semantic `<app>KEY</app><par>INSTANCE</par>`. When speech recognition answers an alternative, the controller starts that instance. |
| Schedule | The `schedules` in the settings, sent in `scheduleConfig`. |
| `appctl.py run` | The server sends a `scheduleConfig` with the settings' schedules and one date schedule 3 seconds in the future, in the rabbit's local time. The rabbit removes the date schedule after it starts the application. |

`run` sends only one `scheduleConfig`, before the application starts. A second `scheduleConfig` in the first seconds can reset the rabbit and stop the application, because the application is not interactive until its `karotz.start` connects. `run` calculates the local time with Python's time-zone database. If Python has no time-zone database (Windows without the `tzdata` package), it uses the local time of your computer. This is correct only if the computer and the rabbit are in the same time zone.

### Options of appctl.py

`appctl.py` connects to the control port, sends one command, prints the result and stops. The options come before the command.

| Option | Default | What it does |
|---|---|---|
| `--port N` | `9124` | The control port of the server (its `--control-port`). |
| `--socket PATH` | none | The Unix socket of the server (its `--control-socket`). |

`appctl.py` without a command prints a short usage. `appctl.py help` or `--help` lists the commands. `appctl.py help COMMAND` explains one command. The commands `say`, `say-in`, `play`, `record`, `photo`, `choreography` and `send` join all their words, so `play title::Little Wing` needs no quotes.

The control protocol is one JSON line for each request, `{"cmd": "...", "args": [...]}`, and one JSON line for the answer, `{"ok": true, "result": ...}` or `{"ok": false, "error": "..."}`.

### Commands of appctl.py

The commands marked "yes" need interactive mode: run `take` first. Without interactive mode, they give the error "not in interactive mode".

| Command | Arguments | Interactive | What it does |
|---|---|---|---|
| `status` | | no | Shows `connected`, `logged_in`, `interactive_id` and the application keys. |
| `take` | `[SECONDS]` | no | Starts interactive mode with a new id. `SECONDS` is the time-out, default 900. |
| `release` | | no | Stops interactive mode. The rabbit resets. |
| `shell` | | no | Starts the interactive shell. |
| `help` | `[COMMAND]` | no | Lists the commands, or explains one. |
| `info` | | no | Sends `karotzinfo_query` and shows the rabbit's reply. |
| `events` | `[N]` | no | Shows the last N messages from the rabbit, default 20. |
| `run` | `APP [INSTANCE]` | no | Starts an application in about 3 seconds. `INSTANCE` is the first instance in the settings file if not given. An unknown instance is an error. Needs `timezone`. |
| `reload` | | no | Reads the settings file again and sends the configuration again. |
| `system` | `COMMAND` | no | Sends `system` with `TELNET_ON`, `TELNET_OFF`, `LOG_ON`, `LOG_OFF`, `LOG_REMOTE_ON`, `LOG_REMOTE_OFF` or `SYSTEM_REBOOT`. The rabbit does not answer. |
| `messages` | | no | Lists the messages in the message store. |
| `pending` | `[on\|off]` | no | Sets the pending-message flag. Without an argument, the flag is on while a message is unread. |
| `contacts` | | no | Sends the contact list again. |
| `led` | `COLOUR` | yes | Lights the LED. `COLOUR` is six hexadecimal digits, `RRGGBB`. |
| `fade` | `COLOUR [MS]` | yes | Fades the LED to the colour in MS milliseconds, default 1000. |
| `pulse` | `COLOUR [MS] [LENGTH]` | yes | Pulses the LED, MS milliseconds for each pulse (default 500), for LENGTH milliseconds (default -1: no end). |
| `ears` | `LEFT RIGHT` | yes | Moves the ears to two positions. 17 steps make one full turn. |
| `ears-reset` | | yes | Moves the ears to the start position. |
| `say` | `TEXT...` | yes | Speaks the text in the rabbit's language. |
| `say-in` | `LANGUAGE TEXT...` | yes | Speaks the text in a language, for example `fr-FR`. |
| `say-stop` | | yes | Stops the speech. |
| `listen` | `LANGUAGE WORD...` | yes | Starts speech recognition for one of the words. |
| `play` | `WHAT` | yes | Plays a URL, a file on the rabbit (`file:///...`) or music from the USB key (`dir::`, `artist::`, `genre::`, `title::`, `playlist::`, `allsong::`; settings `mode::...`, `lock::...`). |
| `stop` | | yes | Stops the playing or the recording. |
| `pause` | | yes | Pauses the playing. |
| `resume` | | yes | Continues the playing. |
| `next` | | yes | Goes to the next song. |
| `previous` | | yes | Goes to the previous song. |
| `artists` | | yes | Lists the artists on the USB key. |
| `folders` | | yes | Lists the folders on the USB key. |
| `genres` | | yes | Lists the genres on the USB key. |
| `songs` | | yes | Lists the songs on the USB key. |
| `playlists` | | yes | Lists the playlists. |
| `photo` | `WHERE` | yes | Takes a photo. The rabbit posts it to the URL, for example `http://192.168.1.10:8080/upload/photo`. |
| `record` | `[URL]` | yes | Records from the microphone to `/tmp/record.flac` on the rabbit until `stop`. The URL goes into the message but the rabbit does not use it. |
| `choreography` | `FILE` | yes | Plays a choreography file. |
| `ping` | | yes | Sends `ping` and shows the reply. |
| `app-log` | `[SINCE]` | no | Shows the reports that applications sent to `/log` after the first SINCE reports, and the count. The server keeps the last 1000. |
| `asr-answer` | `[first\|nomatch\|WORDS...]` | no | Sets what the speech-recognition server answers. Default `first`. Needs `--asr-port`. |
| `take-as` | `ID [SECONDS]` | no | Starts interactive mode with the given id. The controller refuses the empty id (`""`) with `BAD_INTERACTIVEID`. |
| `login` | | no | Sends the authentication token. For a server started with `--hold`. |
| `send` | `MESSAGE...` | no | Sends a `PlatformMessage` in text format. The server adds the interactive id if you are in interactive mode. |
| `send-raw` | `[--no-id] [--cut N] MESSAGE...` | no | Sends a message that can be incorrect: `--no-id` removes the id, `--cut N` removes the last N bytes, and required fields can be missing. The controller answers `ERROR`. |

The server also accepts `events-since [N]`, which the shell uses. It is not in the help.

Examples:

```
uv run python appctl.py say-in fr-FR bonjour
uv run python appctl.py pulse FFFF00 500 5000
uv run python appctl.py send serial { open { port: "/dev/ttyUSB0" baud: 9600 } }
uv run python appctl.py send-raw --cut 1 led { light { color: "FF0000" } }
```

### The interactive shell

`appctl.py shell` shows the state of the rabbit, then the prompt `rabbit>`. Each line is one command, the same as after `appctl.py`, split by shell rules. One time each second, the shell asks the server for new messages from the rabbit and shows each one with `<-`, without the message ids. `help`, `help COMMAND` and `?` show the help. `quit`, `exit`, Ctrl-D (Ctrl-Z then Enter on Windows) or Ctrl-C stop the shell. Line editing and history work where Python has `readline`.

### HTTP endpoints

All endpoints are on the HTTP port (`--http-port`, default 8080).

| Method and path | What it does |
|---|---|
| `GET /file/<apiKey>/<version>/get` | Sends the application as a zip. The version in the path is not checked. An unknown key gives 404. |
| `GET /text` | Sends the text `hello from the app server`, for tests of `http.get`. |
| `GET /log?msg=TEXT` | Prints `app: TEXT` on the console and keeps it for `app-log`. |
| `POST /log` | The same, with the body as the text. |
| `POST /upload` or `POST /upload/NAME` | Saves the body in the uploads folder. A `multipart/form-data` body: each part with a file name goes to `<date>-<time>-<NAME>-<file name>`, and the other fields are only logged. Another body goes to `<date>-<time>-<NAME>.bin`. `NAME` is `upload` if not given. |
| `GET /asr/grammar/<id>.abnf` | Sends the launch grammar of the server (ABNF). |
| `GET /api/karotz/<action>` | The REST API. |

Other paths give 404.

### The REST API

The REST API lets an external application control the rabbit over HTTP. It is the API that existing external Karotz applications call. The server always serves it under `/api/karotz/`. The answers are XML with the root element `<VoosMsg>`.

| Request | Parameters | What it does |
|---|---|---|
| `start` | `apikey`, `installid`, `once`, `timestamp`, `signature` | Starts interactive mode for the instance whose `uuid` is `installid` (the first instance if `installid` is empty). The answer has `interactiveId`, `configId` and the accesses. |
| `ears` | `interactiveid`, `left`, `right`, `relative`, `reset` | Moves the ears. |
| `led` | `interactiveid`, `action=light\|fade\|pulse`, `color`, `period`, `pulse` | The LED. `color` is `RRGGBB`. Default `period` is 1000 for fade and 500 for pulse; default `pulse` is -1. |
| `tts` | `interactiveid`, `action=speak\|stop`, `text`, `lang` | Speech. |
| `multimedia` | `interactiveid`, `action=play\|pause\|resume\|stop\|next\|previous\|record`, `url` | Sound. |
| `webcam` | `interactiveid`, `action=photo\|video`, `url` | Takes a photo and posts it to `url`. |
| `asr` | `interactiveid`, `grammar=word\|word`, `lang` | Speech recognition. |
| `choreography` | `interactiveid`, `start`, `file`, `action=stop` | A choreography. |
| `interactivemode` | `interactiveid`, `action=stop` | Stops interactive mode. |

The signature is the base64 HMAC-SHA1 of the other parameters, sorted by name, as `name=value` joined with `&`, with the application's `secret` as the key. The server checks it only when the settings give the application a `secret`. An unknown `apikey`, a bad signature or an unknown instance gives 403. A missing `interactiveid` or a bad parameter gives 400. No reply from the rabbit in 10 seconds gives 504 with the code `TIMEOUT`. A command answer is the rabbit's reply, for example `<VoosMsg>...<response><code>OK</code></response></VoosMsg>`.

The server posts the messages that come later (the events of a request, and the button, RFID and ears callbacks while the application is interactive) as XML to the application's `<callback>` URL.

```
curl "http://localhost:8080/api/karotz/start?apikey=myapp"
curl "http://localhost:8080/api/karotz/led?interactiveid=<id>&action=light&color=00FF00"
```

### The speech server

The speech server makes speech from text for voice-daemon on the rabbit. voice-daemon sends a POST to the URL in `voice.conf` for the language (`[tts] en=http://192.168.1.10:8081/en`). The body is the text in SSML-style `<s>` and `<break>` tags. The server removes the tags, makes the speech, and sends back an MP3 (`audio/mpeg`, 64 kbit/s). The path gives the language; an empty path is `en`. If the speech fails, the answer is 500. The server keeps each MP3 in memory and sends it again for the same text and language.

| Option of `tts_server.py` | Default | What it does |
|---|---|---|
| `--listen ADDR` | `0.0.0.0` | The listen address. |
| `--port N` | `8081` | The port. |
| `--engine E` | `auto` | `auto`, `kokoro`, `espeak` or `windows`. |
| `--models DIR` | `~/.cache/lapinlibre/kokoro` | The folder of the Kokoro model files. |
| `--selftest` | off | Makes one sentence, checks the MP3, prints `OK` or `FAIL` and stops. It uses a free local port, so it can run while a server uses 8081. |

The engines:

| Engine | Languages | What it needs |
|---|---|---|
| Kokoro-82M | `en` (`bf_emma`), `fr` (`ff_siwis`), `es` (`ef_dora`), `it` (`if_sara`), `pt` (`pf_dora`) | The `kokoro-onnx` package, and `kokoro-v1.0.onnx` and `voices-v1.0.bin` with the correct SHA-256 in the models folder. |
| espeak-ng | all languages | The `espeak-ng` command. |
| Windows speech | the installed Windows voices; other languages use the default voice | `powershell.exe` (Windows, or WSL). |

With `auto`, Kokoro speaks its languages. For the other languages (for example `de` and `nl`), the server uses the first of espeak-ng and Windows speech that it finds. With `kokoro`, only Kokoro is used, and other languages fail. The server encodes MP3 with the `lameenc` package, or with `ffmpeg` if `lameenc` is not installed. The server does not start without an MP3 encoder and a speech engine. At the start, it makes one sentence to load the engine, because voice-daemon does not wait long for the first answer.

```
uv run python tts_server.py --port 8081
uv run python tts_server.py --selftest
```

### The speech-recognition server

The speech-recognition server answers voice-daemon's recognition requests with a result that you choose. It does not analyse the audio. voice-daemon connects over TCP to the server in `voice.conf` `[asr]`. It sends the grammar (ABNF text, or a URL of a grammar), then the audio in parts. Each message is an `AsrMsg` (`userland/proto/asr-message.proto`) with a 4-byte length in network byte order before it. When 1.5 seconds of audio have arrived, the server sends one `AsrMsg` with a `Response`.

| Answer setting | What the server answers |
|---|---|
| `first` | The first alternative of the grammar, code `OK`, score 80, with the semantic of that alternative. |
| `nomatch` | Code `NOMATCH`. |
| other text | That text as the words, code `OK`, score 80, with the semantic of the alternative that has the same words, if there is one. |

For a grammar given by URL: when the server runs inside `app_server.py`, a URL that ends in `/asr/grammar/<id>.abnf` gets the launch grammar of the app server. The controller gives this URL with its `domain_app` host. Another URL is downloaded. When the session ends, the server logs the length of the audio. Inside `app_server.py`, it keeps the audio of the last session as `asr-last.raw` in the uploads folder (16-bit mono, 16 kHz).

| Option of `asr_server.py` | Default | What it does |
|---|---|---|
| `--listen ADDR` | `0.0.0.0` | The listen address. |
| `--port N` | `8443` | The port. |
| `--answer A` | `first` | `first`, `nomatch` or the words. |
| `--keep DIR` | none | Keeps the audio of the last session in `DIR/asr-last.raw`. |

`asr_server.py` alone keeps `asr-last.raw` only with `--keep`, and cannot get the launch grammar from the app server. It needs only the standard library.

### How the rabbit finds the server

The install writes the server's address into the rabbit's configuration from `lapinlibre.txt`. `image/usbkey/applysettings.sh` does this.

| Setting in `lapinlibre.txt` | File on the rabbit | What is written |
|---|---|---|
| `SERVER=HOST` | `/usr/etc/conf/karotz.conf` `[karotz]` | `ip_server=HOST`, `port_server=9123`, `url_app=http://HOST:8080`, `domain_app=HOST`, `url_update=http://127.0.0.1:1` |
| (always) | `/usr/etc/conf/account.conf` | If the file is empty or missing: `[account]`, `id=lapinlibre`, `pwd=lapinlibre` |
| `TTS_SERVER=URL`, or `SERVER` | `/usr/etc/conf/voice.conf` `[tts]` | `en`, `fr`, `de`, `es`, `it`, `nl` and `pt`, each `URL/<lang>`. Without `TTS_SERVER`, `URL` is `http://HOST:8081`. |
| `ASR_SERVER=HOST:PORT`, or `SERVER` | `/usr/etc/conf/voice.conf` `[asr]` | `nbserver=1`, `server0=HOST`, `port=PORT`. Without a port, or without `ASR_SERVER`, the port is 8443. |

`url_update` points to a closed port on the rabbit. The controller restarts the rabbit when the update server offers newer versions, and this makes the check fail immediately. `TTS_SERVER` must be a URL like `http://192.168.1.10:8081`, and `ASR_SERVER` must be `host` or `host:port`. An empty setting keeps the rabbit's current value. See [install.md](install.md) for `lapinlibre.txt`.

### Ports

| Port | Program | Who connects | Option |
|---|---|---|---|
| 9123 TCP | `app_server.py` platform | The rabbit's controller | `--port` |
| 8080 TCP (HTTP) | `app_server.py` downloads, endpoints, REST API | The controller, applications, external applications | `--http-port` |
| 9124 TCP, `127.0.0.1` only | `app_server.py` control | `appctl.py` | `--control-port` (or `--control-socket`) |
| 8081 TCP (HTTP) | `tts_server.py` | voice-daemon | `app_server.py --tts-port`, `tts_server.py --port` |
| 8443 TCP | `asr_server.py` | voice-daemon | `app_server.py --asr-port`, `asr_server.py --port` |

The rabbit's configuration uses 9123, 8080, 8081 and 8443. If you change these ports, change the rabbit's configuration too.

### Security

The app server is for your own computer, your own rabbit and a trusted network. These are the facts:

- The platform port, the HTTP port, the speech port and the speech-recognition port listen on `0.0.0.0` by default, so all computers that can reach them can connect. There is no authentication and no TLS.
- The platform port accepts any connection as the controller, and any id and password. A connected rabbit gets the applications, schedules and commands of the server, which include `system` commands such as telnet on and reboot.
- The HTTP port gives the applications' files to all clients. `/upload` writes the posted files into the uploads folder for all clients. `/log` prints text from all clients.
- The REST API accepts commands from all clients. It checks a signature only for an application with a `secret`.
- The speech server makes speech for all clients.
- The control port listens on `127.0.0.1` only, so only programs on the same computer can send commands. It has no authentication.

Run the server on your own machine, on a trusted network. Do not put it on a public server.

### Tests

`tests/app_server/test_app_server.py` starts `app_server.py` with the `protoc` codec on free local ports, with `--asr-port`. It plays the controller: it connects, answers the login, and checks what the server sends. It tests the social keys, the contact list, the pending-message flag, the message queries, `/text`, `/log`, the uploads, `info`, `system`, `pending`, `take-as`, `send-raw`, `events-since`, the commands that need interactive mode, the speech-recognition answers and the launch grammar, and the REST API (bad signature, start, command, callback, stop). The protoc argument defaults to the host protoc of the build, or `protoc` on the `PATH`.
