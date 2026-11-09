# occupancy-service

A process that takes one archive and one camera, runs the models where the frames are, and publishes each sensor's occupancy. It reports what the models see, which is sometimes wrong. It is not a safety interlock.

[LIVE.md](LIVE.md) is the HTTP contract for its live data. Anyone can implement it.

## Install

```sh
uv sync
bin/install-hooks.sh    # refuse commits on main, and run the gate before each push
```

## Run

```sh
uv run python -m occupancy_service --archive <path> --camera /dev/video0 --broker <host>:<port>
```

This starts `Capture`, `L1Loop`, `Publisher` and `LiveServer`, and logs to stderr. With `--detector-model`, `L1Loop` also runs the [detector](#l0). The camera's size is the manifest's `camera.resolution`, read once at the start, so a reload that changes it needs a restart.

| Flag | Goes to | Default |
| :--- | :--- | :--- |
| `--archive` | `L1Settings.archive`, and `LiveServer`'s archive | Required |
| `--camera` | `CaptureSettings.device` | Required |
| `--broker <host>:<port>` | `PublisherSettings.host`, `port` | Required |
| `--profile`, repeated | `PublisherSettings.profiles` | `occupancy` |
| `--address` | `ServerSettings.address` | `127.0.0.1` |
| `--port` | `ServerSettings.port` | `8000` |
| `--engine` | `L1Settings.engine` | `onnxruntime` |
| `--device` | `L1Settings.device` | `CPU` |
| `--frame-rate` | `CaptureSettings.frame_rate` | `20` |
| `--detector-model` | `L1Settings.detector_model` | None: no detector |
| `--detector-engine` | `L1Settings.detector_engine` | `onnxruntime` |
| `--detector-device` | `L1Settings.detector_device` | None: `onnxruntime` takes no device |

Each of these refuses the process: it exits 1 with one line on stderr, with no `ffmpeg` started and no port left bound. A flag that argparse refuses, such as a malformed `--broker`, exits 2.

- An archive that cannot be read, which is anything `r49.load` raises. Without a manifest the service has no sensors, no resolution and no archive id, so it has nothing to do, and it serves nothing. `control`'s timer per sensor reports the silence, and a restart policy picks up a repaired archive with no person restarting anything. A running service that loses its archive keeps its geometry.
- A profile that raises `ProfileError`.
- An address that cannot be bound.

None of these refuses it. The process starts, and the parts report and recover as they do at any other time:

- A camera that is absent: sensors read `no frames`.
- A broker that is absent: it is retried, and `latest` answers meanwhile.
- A model that does not load: sensors read `no model`.
- A detector model that does not load, or a `--detector-model` with `rails49-detector` not installed: `latest` says so in [`detector`](LIVE.md#optional-fields), and L1 comes from the classifier alone.
- A manifest with no `id`: sensors read `archive has no id`.

SIGTERM and SIGINT both stop the parts and exit 0. `service stopped` goes out for every sensor while a broker is connected, and no `ffmpeg` is left behind.

`live-check` tests a running implementation against the contract:

```sh
uv run live-check http://localhost:8000/live
uv run live-check --save http://localhost:8000/live    # also rewrites manifest.json with its own bytes
```

## L1

`occupancy_service.l1` turns the newest frame into L1. It reads the archive's manifest, and only the drift check reads an image. `L1Settings` holds the archive's path, the distance from a track past which a sensor has no track (0.3 G), the time until `scoring stalled` (`stalled_after`, 1 s), the [drift check](#drift)'s interval and tolerance, and the engine, device and model, which default to onnxruntime and the model in the `rails49-classifier` wheel. `CaptureSettings` holds the time until the camera is `lost` (`lost_after`, 1 s).

The `openvino` engine needs the `openvino` extra (`uv sync --extra openvino`). The dev group holds it too, so the tests run it on the CPU. onnxruntime runs only on the CPU, and any other device reads `no model`.

At or above the model's threshold a sensor is `occupied`, and below it `clear`. There is no smoothing across frames. With the detector, [L0](#l0) is combined with this, and can make a sensor `occupied`, or answer `no track` and `no model`. A sensor is `unknown`, with one of these reasons, the first that applies:

| Reason | When |
| :--- | :--- |
| `drift` | The newest [drift reading](#drift) is past the tolerance. This reason is on every sensor, whatever else applies to it. |
| `archive has no id` | The manifest has no `id`. The service keeps running, so that a page can save the archive and mint one. |
| `no model` | The model did not load. |
| `DPT unresolved` | The calibration gives no DPT. |
| `no track` | The sensor is farther than the limit from every track. |
| `no frames` | The camera is `lost`, which takes about 1 s without a frame. Nothing is served from before the loss. |
| `scoring stalled` | The camera is fine, and for about 1 s no result has come. |
| `wrong frame size` | The frame does not match `camera.resolution`. |
| `outside the frame` | The classifier returned `None` for the sensor. |

**The turnout limit:** a sensor near a turnout is scored like any other, but the training data leaves such positions out, so the classifier's accuracy there is not known. The service does not detect these sensors, and reports nothing special for them.

## L0

The detector is `rails49-detector`, which is AGPL and installed separately: no dependency of this package names it, and CI installs it nowhere. `occupancy_service.detector_model` is the one module that imports it, and only when `L1Settings.detector_model` is set. With that empty, the default, the service runs as it would without the detector. The [image](#box) installs it only when asked.

The model loads once, at start, as the classifier's does, and a restart tries again. `latest` reports the load in `detector`: `ok`, or `failed` with the reason. A detector that raises or hangs after a good load stays `ok`: its L0 goes stale, `l0_age_ms` grows, and the log says why.

The detector runs on its own thread, so it never holds up the classifier: it takes the newest frame, runs, and waits `detect_every` (1 s). `detector_engine` and `detector_device` are its own settings. With no DPT, or a frame not at `camera.resolution`, there is no run and no L0. `timings` reports `detect_ms`, `l0_rate_fps` and `l0_age_ms`.

Each classifier result is combined with the newest L0 no older than `l0_limit` (5 s), when the result is read, so a reload takes effect with no new run. Past the limit the detector says nothing, and `latest` has no `l0`. Results that are not the classifier's, such as `no frames` or `drift`, are not combined.

The detector says a sensor is occupied when the sensor lies strictly inside a box. A box keeps a detection's centre and angle, takes the car width, `DPT × 3000 / 1435`, in place of the predicted width, and is `margin_g` (2 G) longer at both ends. Where several boxes cover it, the highest confidence wins. A sensor in the frame and in no box is clear, and the detector says nothing of a sensor outside the frame. [`conformance/l1.json`](LIVE.md#l1-cases) holds these cases, for rails49/occupancy#337 to share.

| Classifier | Detector | L1 |
| :--- | :--- | :--- |
| `occupied` | any | `occupied` with the higher confidence |
| any answer but the reasons in the last row | `occupied` | `occupied` |
| `clear` | `clear`, or no fresh L0 | `clear` |
| `no track` or `no model` | `clear` | `clear` |
| `no track` or `no model` | no fresh L0 | `unknown` with the classifier's reason |
| `no frames`, `scoring stalled`, `wrong frame size`, `DPT unresolved`, `outside the frame`, `archive has no id` | any | `unknown` with the classifier's reason |

`drift` stands over all of these, as in [L1](#l1).

The own-vocabulary [profile](#profile-files) publishes each L0 on `occupancy/<archive id>/l0`.

## Drift

`occupancy_service.drift` measures how far the camera has moved since the archive's `images[0]`, as `@occupancy/drift` in rails49/occupancy does, on the same [shared cases](LIVE.md#drift-cases). It does not correct drift. A person puts the camera back, or authors a new archive.

`L1Settings` holds the interval (`drift_every`, 3 s) and the tolerance in track gauges (`drift_tolerance_g`, 0.5 G, multiplied by the layout's DPT). `drift_tolerance_g = None` turns the check off.

The check takes the newest frame on its own thread, at once and then every interval, so a slow check does not delay L1. Models and sampling keep running through a refusal, so putting the camera back ends it with no restart. One reading decides, with no filter across samples and no override.

`latest` reports the reading in [`drift`](LIVE.md#drift), and every MQTT profile carries the refusal as it carries any `unknown`, so a profile's hold applies to the `clear` that follows.

| `unmeasured` | When |
| :--- | :--- |
| `no reading yet` | The check has not read a frame yet. |
| `no first image` | The manifest has no images, or the archive does not hold the first. |
| `DPT unresolved` | The calibration gives no DPT. |
| `check failed` | The first image or a frame does not decode, the first image is too small to hold a block, or the check raised. A failed first image stays so until a reload, and a failed frame until the next sample. |

With any of these, L1 goes on as if the camera had not moved. A reload rebuilds the check, on the thread that samples, from the new `images[0]`. The last reading, and any refusal, stands until the next one, unless the reload leaves nothing to measure against.

A reading is in pixels of the first image and DPT is in `camera.resolution` pixels, so the two compare directly only when the first image is at `camera.resolution`.

## HTTP

`occupancy_service.server` serves [LIVE.md](LIVE.md) under a fixed `/live`, with a thread per request. `ServerSettings` holds the address and port, which default to `127.0.0.1:8000`, and the limits of `events`. The caller owns the `L1Loop`. There are no `Access-Control-*` headers. Only a write checks `Origin`.

| Route | Answer |
| :--- | :--- |
| `GET /live/latest` | The newest scored frame, not the newest captured one. It does not wait: with no frame to serve it is the JSON part alone. |
| `GET /live/latest?after=<frame id>` | The same, once there is a newer frame, or after about 1 s. An id from before a restart counts as older than any frame. |
| `GET /live/latest?patches=1` | Also one PNG part per sensor with a patch, encoded only then. |
| `GET /live/events` | Every sensor's state, then each new state or reason, with a comment line every 15 s. |
| `GET /live/archive/<path>` | The file, through `rails49-r49`'s `image()`, from a zip or a folder. What it reports absent, or a path with a segment that starts with a dot, is a 404. |

`PUT` and `DELETE` under `/live/archive/` [save back to a folder archive](LIVE.md#saving). `occupancy_service.archive_writer` does the writing, because `rails49-r49` only reads: each file goes to a dot-named temp file in its folder, is flushed to disk, then renamed. A saved manifest reloads the `L1Loop`. `occupancy_service.origin` holds a copy of `is_own_page` from `control`, with the code unchanged, and a change there is copied here by hand.

`latest`, `events` and `manifest.json` are `no-store`. `frame_id` holds a token for this run of the service, and readers treat it as opaque.

A frame not at `camera.resolution` is left out of `latest`, because the contract has the frame part at that size, and each sensor says `wrong frame size`. With no frame served, `timings` holds only `rate_fps`.

An `events` client that takes over 2 s over one write, or has over 1000 changes waiting, is dropped, and gets the full state when it reconnects. Each client waits on its own thread, so it holds up neither the others nor the scoring loop.

A client that closes or resets a connection ends that connection's thread, and nothing is printed.

## MQTT

`occupancy_service.publisher` publishes L1 as its active profiles say, over `paho-mqtt`: one connection for every profile whose `connection` is `shared`, and [one per sensor](#one-connection-per-sensor) on each profile whose `connection` is `per_sensor`. `PublisherSettings` holds the broker's host and port, with no authentication, and the active profiles, which default to the own-vocabulary profile only. It also holds how often to retry a lost broker (1 s), the MQTT keepalive of every connection (5 s), and the timeout of connecting (2 s). A broker gives up on a silent connection after one and a half keepalives, and then sends its [last will](#one-connection-per-sensor), so a will goes out within about 8 s of a host or a network failing. A killed process closes its connections, and the broker sends each will at once. The [command](#run) passes `publisher.last_messages` to `LiveServer`, so `latest` reports them.

Every sensor goes out on each connect of the connection that carries it and on each repeat, and each change goes out at once, except a `clear` that a profile [holds](#holding-clear). A change is a new state or a new reason. A profile with `repeat = 0` does not repeat, and sends a change, a clean stop's `unknown` or a removed sensor's `unknown` only if its payload differs from the last one sent for that sensor. Sensors and the archive's `id` come from the newest result each time, so a reload needs no restart, and a sensor the manifest no longer names stops being reported. Until the first result every sensor is `unknown`, as [L1](#l1) says, and a repeat is never older than `scoring stalled` allows. A clean stop publishes `unknown` with the reason `service stopped` for every sensor, then disconnects. A lost broker is retried with no restart.

### Profile files

A profile is a TOML file. The `profiles` setting names each by a shipped profile's name, or by a path holding a `/` or ending in `.toml`. A profile is called by its file's name, and two active ones cannot share a name. A file with anything wrong or unknown in it stops the `Publisher` being made.

| Key | Holds |
| :--- | :--- |
| `topic` | The topic template. It may hold `{sensor}` and `{archive_id}`. |
| `payload.occupied`, `payload.clear`, `payload.unknown` | What to send. A table is sent as JSON, and a string as text. Each may hold `{sensor}`, `occupied` also `{confidence}`, and `unknown` also `{reason}`. |
| `retain` | `true` or `false`. |
| `qos` | 0, 1 or 2. |
| `repeat` | Seconds between sending every sensor, from 0. 0 means no repeat: every sensor still goes out on each connect, and anything else only if its payload differs from the last one sent for that sensor. |
| `clear_hold_s` | Optional. Seconds to [hold](#holding-clear) a `clear`, from 0. Absent or 0 means no hold. |
| `connection` | Optional. `shared`, the default, or `per_sensor` for [one connection per sensor](#one-connection-per-sensor). |
| `l0_topic` | Optional, and only with `shared`. The topic of [L0](#l0), which may hold `{archive_id}`. Absent means no L0. |
| `will_state` | Optional, and only with `per_sensor`. `occupied`, `clear` or `unknown`: the state whose payload is each sensor's last will, on that sensor's topic, with the profile's `retain` and `qos`. A `{reason}` in it is `connection lost`, and it cannot hold `{confidence}`. Absent means no will. |

In a table, a string that is only `{confidence}` becomes a JSON number. Anywhere else a placeholder becomes text.

A profile with an `l0_topic` publishes one message per detector run there, never retained, at the profile's `qos`, and nothing while the archive has no `id`. The message is the [`l0` object of `latest`](LIVE.md#l0) without its `frame_id`, which only `latest` can give. L0 does not repeat, and does not go out on a connect.

A topic that holds `{archive_id}` publishes nothing while the archive has no `id`. A sensor id or archive `id` that a topic holding it cannot carry, because it has `+`, `#`, `/`, a control character or a Unicode non-character, or makes a topic MQTT does not allow, is logged once, and that sensor is not published on that profile. The other sensors and profiles keep reporting.

### One connection per sensor

A profile with `connection = "per_sensor"` opens one connection for each sensor it can report, with the keepalive in `PublisherSettings` (5 s). Its client id is `occupancy-service-<profile>-<archive id>-<sensor>`, or `occupancy-service-<profile>-<sensor>` while the archive has no `id`. With a `will_state`, the broker publishes that state's payload on the sensor's topic when the connection ends without a clean disconnect: when the process is killed, or its host or network fails. A shared connection has no will.

- A sensor a reload adds gets its connection with no restart, and sends its state once that connection is up.
- A sensor a reload removes sends `unknown` with the reason `sensor removed`, then disconnects cleanly, so the broker keeps that `unknown` where the profile is retained.
- When the archive's `id` changes, each sensor's connection disconnects cleanly, and a new one, whose client id holds the new `id`, sends the sensor's state once it is up. Only where the sensor is gone, or its topic holds `{archive_id}` and so changes too, does the old connection first send `unknown` with the reason `sensor removed` on the old topic. A sensor whose topic stays the same sends nothing first, so a `clear` sensor does not read `true` for a moment on Rocrail, which takes it as a ghost train, or `ACTIVE` on JMRI.
- A clean stop sends `unknown` with the reason `service stopped` on each connection, then disconnects each cleanly.

A hung process whose connections stay up sends nothing, and the broker sends no will, as its paho threads go on answering the keepalive. Only a profile's repeat stopping shows that.

### Holding `clear`

One frame the classifier gets wrong would put `clear` on the topic in the middle of a train. Control takes the first `clear` after `occupied` on an entry sensor for the train's tail, so a profile can hold `clear` back.

The hold lowers how often a sensor reads `clear` in the middle of a train and does not rule it out: a `clear` lasting longer than the hold is still published.

With `clear_hold_s` set, a sensor whose last reported state on that profile is not `clear` publishes `clear` only once every result has read `clear` for that long. The time runs from the capture time of the first `clear` frame, and any other result restarts it, including one that came while the broker was gone. `clear` goes out on the first result captured at or after the end of the hold, so with the frames stopped it does not go out: a sensor goes `unknown` first.

- Until then the profile keeps reporting the last state it published, with its `reason` if it has one, on each repeat and each connect. A sensor that was `unknown` stays `unknown`, so a wrong `clear` right after an outage is held too.
- `occupied` and `unknown` go out at once, as do `unknown` at startup and on a clean stop.
- The state is kept per sensor and per profile. A sensor with nothing reported yet, because a reload added it or because it was already `clear` when the service connected, says `unknown` with the reason `clear not yet held`. A sensor a reload removes is forgotten.
- `last_messages` in `latest` shows what the profile sent, so it can differ from L1 for up to the hold. L1 and the rest of the [live contract](LIVE.md) do not change.

The hold makes `clear` later, so control releases a block or transit up to that much later. Where a far sensor is `unknown`, control stops a train on the entry sensor's `clear`, so the train runs on by its speed times the hold: about 10 cm at 104 mm/s and 1 s.

### The shipped profiles

`occupancy`, this service's own vocabulary. Each message is an [L1 entry](LIVE.md#l1-entry). With the detector installed, it also publishes L0 on `occupancy/<archive id>/l0`:

```toml
topic = "occupancy/{archive_id}/sensor/{sensor}"
retain = false
qos = 0
repeat = 1
l0_topic = "occupancy/{archive_id}/l0"

[payload.occupied]
state = "occupied"
confidence = "{confidence}"

[payload.clear]
state = "clear"

[payload.unknown]
state = "unknown"
reason = "{reason}"
```

`control`, whose contract is rails49/control#1147. It has no last will, because `layout` has a timer per sensor. That timer wants every sensor at least once a second, so `repeat` leaves room for one to be late. It holds `clear` for 1 s, which rails49/occupancy-service#18 measures on real trains:

```toml
topic = "tc49/layout/sensor"
retain = false
qos = 0
repeat = 0.9
clear_hold_s = 1

[payload.occupied]
addr = "{sensor}"
occupancy = "occupied"

[payload.clear]
addr = "{sensor}"
occupancy = "clear"

[payload.unknown]
addr = "{sensor}"
occupancy = "unknown"
reason = "{reason}"
```

`jmri`, for JMRI's MQTT sensors. It is not active by default:

```toml
topic = "track/sensor/{sensor}"
retain = true
qos = 0
repeat = 1
connection = "per_sensor"
will_state = "unknown"

[payload]
occupied = "ACTIVE"
clear = "INACTIVE"
unknown = "ACTIVE"
```

A string payload is written the same way, as `clear = "OFF {sensor}"` under `[payload]`.

### JMRI

Turn it on with `--profile jmri`, or with `--profile occupancy --profile jmri` to keep the own vocabulary too. In JMRI, add an MQTT connection to the same broker, and add a sensor for each sensor in the archive. A sensor's `id` must be JMRI's name after `MS`, so `C3.A` is the sensor `MSC3.A`. JMRI's MQTT channel is blank by default since JMRI 5.1.2. If one is set, copy the profile to a file that puts the channel before `track/sensor/{sensor}`, and use that file.

- `occupied` is `ACTIVE` and `clear` is `INACTIVE`. `unknown` is `ACTIVE` too: JMRI's `MqttSensor` takes only `ACTIVE` and `INACTIVE`, and for anything else logs a warning and keeps the state it had. A sensor JMRI cannot see is taken as occupied.
- Each message is retained, so JMRI gets every sensor's state when it connects.
- A person who clicks a sensor in JMRI publishes retained on the same topic. The repeat puts the service's state back within about 1 s.
- Each sensor has its own connection, whose last will is `ACTIVE`. A service that is killed, or whose host or network fails, leaves every sensor `ACTIVE`: at once for a killed process, and within about 8 s for a failure the broker notices only by the keepalive. A clean stop and a removed sensor leave `ACTIVE` too.
- With no archive `id`, every sensor reads `archive has no id`, which is `ACTIVE`.

JMRI does not notice a hung service whose connections stay up: its sensors keep the last state the service sent.

`rocrail`, for Rocrail's sensors. It is not active by default:

```toml
topic = "rocrail/service/field"
retain = false
qos = 1
repeat = 0
connection = "per_sensor"
will_state = "unknown"

[payload]
occupied = '<fb id="{sensor}" state="true"/>'
clear = '<fb id="{sensor}" state="false"/>'
unknown = '<fb id="{sensor}" state="true"/>'
```

### Rocrail

Turn it on with `--profile rocrail`, or with `--profile occupancy --profile rocrail` to keep the own vocabulary too. In Rocrail, set the MQTT broker's host and port to the service's broker, as the [wiki's services page](https://wiki.rocrail.net/doku.php?id=rocrailini-service-en) says. A sensor's `id` must be Rocrail's sensor ID, and should avoid `"`, `<` and `&`: the service does not escape it, and Rocrail drops a message it cannot parse. `rocrail/service/field` needs no support key and no address or interface on the sensor. This was checked on Rocrail 26.05.15.

- `occupied` is `true` and `clear` is `false`. `unknown` is `true` too, as Rocrail has only the two: a sensor the service cannot see is taken as occupied.
- Nothing is retained and nothing repeats, because Rocrail takes a repeated `true` as a new event. A change goes out only when it turns `true` into `false` or back, so `occupied` to `unknown`, or a new reason, sends nothing.
- Each sensor has its own connection, whose last will is `true`. A service that is killed, or whose host or network fails, turns every sensor `true`: at once for a killed process, and within about 8 s for a failure the broker notices only by the keepalive. A clean stop leaves every sensor `true` too.
- A sensor the manifest removes is sent `true`, if it was not already, and stays `true` in Rocrail.
- With no archive `id`, every sensor reads `archive has no id`, which is `true`.

In automatic mode, `unknown`, or a false detection, on a block with no train is a ghost train, and by default Rocrail then turns off track power until a person turns it on. That includes a block just released while its sensor stays `true`, as a train's tail leaves it. With `poweroffatghost="false"` on `ctrl` in `rocrail.ini`, a ghost closes only that block, and a running train continues. Outside automatic mode no ghost is reported.

- Start the service before turning on automatic mode, so every sensor has its state first.
- Restart the service after restarting Rocrail. With no repeat and no retain, Rocrail keeps the states it had until the next change. This is expected, and not yet tested.

Rocrail does not notice a hung service whose connections stay up, nor a lost broker: its sensors keep the last state the service sent.

## Box

`compose.yaml` runs the service and the [occupancy page](https://github.com/rails49/occupancy) behind the box's door. Both containers join the installation's `rails49` network and publish no ports. `occupancy.$BOX_DOMAIN` goes to the page, and `/live` on that host goes to the service, with the prefix kept. The page talks to the service on its own origin.

The installation runs first: it creates the network and runs `broker`, which the service dials on port 1883. Then, from a clone of this repository:

```sh
OCCUPANCY_ARCHIVE=/srv/rails49/layout.r49 \
OCCUPANCY_CAMERA=/dev/v4l/by-id/usb-<camera>-video-index0 \
TC49_UID=$(id -u) TC49_GID=$(id -g) \
VIDEO_GID=$(getent group video | cut -d: -f3) \
RENDER_GID=$(getent group render | cut -d: -f3) \
docker compose --env-file /etc/rails49/box.env -f compose.yaml up -d --build
```

| Setting | Holds | Default |
| :--- | :--- | :--- |
| `BOX_DOMAIN` | The box's domain, from `/etc/rails49/box.env` | None |
| `OCCUPANCY_ARCHIVE` | The archive's path on the box, mounted read-write | None |
| `OCCUPANCY_CAMERA` | The camera's path, under `/dev/v4l/by-id/` | None |
| `TC49_UID`, `TC49_GID` | The user and group the service runs as, so the files it saves are the person's | None |
| `VIDEO_GID`, `RENDER_GID` | The host's `video` and `render` groups, for the camera and the GPU | None |
| `DETECTOR_VERSION` | The `rails49-detector` version the image installs | Empty: no detector |

Compose names a missing setting and starts nothing.

The service runs the `occupancy` and `control` profiles, with OpenVINO on the GPU. It restarts unless stopped, so an archive that could not be read is picked up again once it is repaired. The host's `/dev` is mounted, with video devices and the GPU allowed, so a camera that is unplugged and plugged back in appears with no restart.

The page can save only to an archive that is a folder named `*.r49`. A zip is served, and refuses saves. To make a folder from a zip:

```sh
mv layout.r49 layout.zip && unzip layout.zip -d layout.r49
```

The page is built from the `rails49/occupancy` commit in `OCCUPANCY_COMMIT` in `compose.yaml`, and nginx sets the headers its `_headers` names. Moving to a new commit is a PR that changes that line.

`DETECTOR_VERSION=0.0.2` on the start command builds the service image with that version of `rails49-detector` (AGPL) from PyPI. Running it also needs `--detector-model` in the service's `command`, which rails49/occupancy-service#13 sets.

Two steps belong to the box and are not code here:

- An `A` record for the `occupancy` label. The door gets a certificate per router, and holds no wildcard.
- `occupancy` in `BOX_UIS` in `/etc/rails49/box.env`, so the installation's page links the UI.

## Test

```sh
bin/test.sh             # Ruff, Black, Pyright and pytest
```

`tests/test_compose.py` runs `docker compose config` and reads the project it resolves, which needs the docker CLI and no daemon. `tests/test_images.py` runs against built images, and only when `OCCUPANCY_SERVICE_IMAGE` and `OCCUPANCY_PAGE_IMAGE` name them. CI's `images` job builds both, the service's without the detector, and sets them.

The L1 tests use a fake capture source and the packaged model, on the CPU. The detector's tests use a fake detector, and the tests of the command a fake `detector` module on the child's path, so no AGPL package is installed. The drift tests run the [shared cases](LIVE.md#drift-cases), and the real check on the real loop. The server tests run real HTTP and `live-check` against the real loop, on the same fake source. The capture tests use a fake `ffmpeg`, so they need no camera. One test runs the real `ffmpeg` command line, and is skipped when `ffmpeg` is not on the `PATH`. The MQTT tests run a real `mosquitto`, which they start themselves on a free port, and are skipped when it is not installed. CI installs both. The tests of the command run it as a real child process, with the fake `ffmpeg` on the `PATH`.
