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

This starts `Capture`, `L1Loop`, `Publisher` and `LiveServer`, and logs to stderr. With `--detector-model`, `L1Loop` also runs the [detector](#l0). The camera's size is the open archive's `camera.resolution`, and with no archive the largest MJPEG size `ffmpeg -list_formats` gives for the camera. Each archive `L1Loop` opens, after a save too, sets it, and a new size starts `ffmpeg` again.

`--camera` is a V4L2 device path, or the URL of an MJPEG stream: any value with `://` in it. A stream is not asked for a size or a rate: its source sets both, so `--frame-rate` does not apply to it, and its frames must already be at the archive's `camera.resolution`. See [A camera that is not a USB webcam](#a-camera-that-is-not-a-usb-webcam).

`--archive` may hold `{railroad}`, as in `--archive=/archives/{railroad}.r49`. Then the service starts with no archive, and opens the loaded railroad's archive once a profile's [railroad topic](#profile-files) names it. A new name, or a new stamp on the same name, opens that railroad's archive for the next frame, as a save does: `events` clients are closed at its first result. A row with `built: false` or an empty name is ignored. A name with a `/` in it has no archive. With no profile naming a railroad topic, `{railroad}` stays unfilled and the service runs with no archive. `occupancy_service.railroad` follows the topic on a connection of its own. A lost broker changes nothing, and the retained row comes again on the next connection.

With no archive at the path, or none yet for the railroad, the service runs, publishes no L1 and no L0, and answers as [LIVE.md](LIVE.md#no-archive) says. A path with no `{railroad}` is the archive whatever railroad is loaded, and the railroad's name is still followed for that answer.

| Flag | Goes to | Default |
| :--- | :--- | :--- |
| `--archive` | `L1Settings.archive`, or with `{railroad}` in it `RailroadSettings.archive` | Required |
| `--camera` | `CaptureSettings.device`: a device path or an MJPEG stream URL | Required |
| `--broker <host>:<port>` | `PublisherSettings.host`, `port` | Required |
| `--profile`, repeated | `PublisherSettings.profiles` | `occupancy` |
| `--address` | `ServerSettings.address` | `127.0.0.1` |
| `--port` | `ServerSettings.port` | `8000` |
| `--engine` | `L1Settings.engine` | `auto` |
| `--device` | `L1Settings.device` | `auto` |
| `--frame-rate` | `CaptureSettings.frame_rate`, for a device only | `20` |
| `--detector-model` | `L1Settings.detector_model` | None: no detector |
| `--detector-engine` | `L1Settings.detector_engine` | `auto` |
| `--detector-device` | `L1Settings.detector_device` | `auto` |
| `--power-topic` | `PowerSettings.topic`, on the `--broker` | None: never [paused](#track-power) |

`auto` lets the service pick, and any other value is used as given. For the classifier:

- OpenVINO on `GPU`, when `openvino` imports, lists a device named `GPU`, and the model loads there. `GPU.0` and `GPU.1` do not count.
- Else OpenVINO on `CPU`, when `openvino` imports.
- Else onnxruntime on `CPU`.

For the detector, OpenVINO on `CPU` when `openvino` imports, else onnxruntime with no device. `latest` reports that as `CPU`, where `rails49-detector` runs a model given no device. It never tries the GPU, which rails49/occupancy-service#13 measures. An explicit `--detector-device=GPU` still works.

One log line at start names the classifier's engine and device, with the error when it did not load on a listed GPU. Another names the detector's, when one loads. `latest` reports them in [`classifier` and `detector`](LIVE.md#optional-fields).

Each of these refuses the process: it exits 1 with one line on stderr, with no `ffmpeg` started and no port left bound. A flag that argparse refuses, such as a malformed `--broker`, exits 2.

- An archive that cannot be read, which is anything `r49.load` raises but for a path with nothing at it, including a manifest with no `id`. `control`'s timer per sensor reports the silence, and a restart policy picks up a repaired archive with no person restarting anything. A running service that loses its archive keeps its geometry. A railroad's archive that cannot be read is logged, and leaves the service with no archive.
- A profile that raises `ProfileError`, and two active profiles that name different railroad topics.
- An address that cannot be bound.

None of these refuses it. The process starts, and the parts report and recover as they do at any other time:

- A camera that is absent: sensors read `no frames`.
- A broker that is absent: it is retried, and `latest` answers meanwhile.
- A model that does not load: sensors read `no model`.
- A detector model that does not load, or a `--detector-model` with `rails49-detector` not installed: `latest` says so in [`detector`](LIVE.md#optional-fields), and L1 comes from the classifier alone.

SIGTERM and SIGINT both stop the parts and exit 0. `service stopped` goes out for every sensor while a broker is connected, and no `ffmpeg` is left behind.

`live-check` tests a running implementation against the contract:

```sh
uv run live-check http://localhost:8000/live
uv run live-check --save http://localhost:8000/live    # also rewrites manifest.json with its own bytes
```

## L1

`occupancy_service.l1` turns the newest frame into L1. It reads the archive's manifest, and only the drift check reads an image. `L1Settings` holds the archive's path, the distance from a track past which a sensor has no track (0.3 G), the time until `scoring stalled` (`stalled_after`, 1 s), the [drift check](#drift)'s interval and tolerance, and the engine, device and model, which default to `auto` and the model in the `rails49-classifier` wheel. `CaptureSettings` holds the time until the camera is `lost` (`lost_after`, 1 s).

The `openvino` engine needs the `openvino` extra (`uv sync --extra openvino`). The dev group holds it too, so the tests run it on the CPU. onnxruntime runs only on the CPU, and any other device reads `no model`.

At or above the model's threshold a sensor is `occupied`, and below it `clear`. There is no smoothing across frames. With the detector, [L0](#l0) is combined with this, and can make a sensor `occupied`, or answer `no track` and `no model`. A sensor is `unknown`, with one of these reasons, the first that applies:

| Reason | When |
| :--- | :--- |
| `drift` | The newest [drift reading](#drift) is past the tolerance. This reason is on every sensor, whatever else applies to it. |
| `no model` | The model did not load. |
| `DPT unresolved` | The calibration gives no DPT. |
| `no track` | The sensor is farther than the limit from every track. |
| `no frames` | The camera is `lost`, which takes about 1 s without a frame. Nothing is served from before the loss. |
| `track power off` | Scoring is [paused](#track-power), or has resumed and no result has come yet. |
| `scoring stalled` | The camera is fine, and for about 1 s no result has come. |
| `wrong frame size` | The frame does not match `camera.resolution`. |
| `outside the frame` | The classifier returned `None` for the sensor. |

**The turnout limit:** a sensor near a turnout is scored like any other, but the training data leaves such positions out, so the classifier's accuracy there is not known. The service does not detect these sensors, and reports nothing special for them.

## L0

The detector is `rails49-detector`, which is AGPL and installed separately: no dependency of this package names it, and the gate installs it nowhere. `occupancy_service.detector_model` is the one module that imports it, and only when `L1Settings.detector_model` is set. With that empty, the default, the service runs as it would without the detector. The [image](#box) installs it at a pinned version.

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
| `no frames`, `scoring stalled`, `wrong frame size`, `DPT unresolved`, `outside the frame` | any | `unknown` with the classifier's reason |

`drift` stands over all of these, as in [L1](#l1).

The own-vocabulary [profile](#profile-files) publishes each L0 on `occupancy/<archive id>/l0`.

## Track power

With `--power-topic`, `occupancy_service.power` follows control's track power on a connection of its own to the broker, and pauses the `L1Loop` while the newest message there reads `off`. Control publishes it retained, as `{"power": "on" | "draining" | "off", ...}`, on `tc49/layout/state/power`. `on`, `draining`, a message that cannot be read and a lost connection all resume it, so a fault never leaves scoring paused.

While paused, no frame is decoded, scored, given to the detector or checked for drift. `ffmpeg` keeps running and the models stay loaded, so the first result after a resume comes with the next frame. `latest` has no frame part, and until that first result a sensor reads `track power off`, unless a reason above it in [L1](#l1)'s table applies. The profiles keep repeating, so control's timer per sensor gets its reports.

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
| `GET /live/archive/<path>` | The file, through `rails49-r49`'s `image()`, from a zip or a folder. What it reports absent, or a path with a segment that starts with a dot, is a 404. With no archive, `manifest.json` is a 404 with a [JSON body](LIVE.md#no-archive). |

`PUT` and `DELETE` under `/live/archive/` [save back to a folder archive](LIVE.md#saving). `occupancy_service.archive_writer` does the writing, because `rails49-r49` only reads: each file goes to a dot-named temp file in its folder, is flushed to disk, then renamed. A saved manifest reloads the `L1Loop`. `PUT /live/archive.r49` [creates the archive](LIVE.md#creating-an-archive) at `--archive`'s path, the loaded railroad's name in place of `{railroad}`, where nothing is: `ArchiveCreator` writes the zip to a dot-named temp file, unpacks it into a dot-named folder beside that path, and renames the folder to it. The `L1Loop` then opens it. `occupancy_service.origin` holds a copy of `is_own_page` from `control`, with the code unchanged, and a change there is copied here by hand.

`latest`, `events` and `manifest.json` are `no-store`. `frame_id` holds a token for this run of the service, and readers treat it as opaque.

A frame not at `camera.resolution` is left out of `latest`, because the contract has the frame part at that size, and each sensor says `wrong frame size`. With no frame served, `timings` holds only `rate_fps`.

An `events` client that takes over 2 s over one write, or has over 1000 changes waiting, is dropped, and gets the full state when it reconnects. Each client waits on its own thread, so it holds up neither the others nor the scoring loop.

A client that closes or resets a connection ends that connection's thread, and nothing is printed.

## MQTT

`occupancy_service.publisher` publishes L1 as its active profiles say, over `paho-mqtt`, on one connection that every profile shares. `PublisherSettings` holds the broker's host and port, with no authentication, and the active profiles, which default to the own-vocabulary profile only. It also holds how often to retry a lost broker (1 s), the MQTT keepalive (5 s), and the timeout of connecting (2 s). The connection has no last will: [When the service dies or hangs](#when-the-service-dies-or-hangs) says what each controller sees. The [command](#run) passes `publisher.last_messages` to `LiveServer`, so `latest` reports them. What each active profile sends, holds and last sent is an `ActiveProfile`, in `occupancy_service.active_profile`; `Publisher` keeps the thread and the connection, and hands each pass to a `Sender`, which sends L0 and runs each `ActiveProfile`.

Every sensor goes out on each connect and on each repeat, and each [L1 change](GLOSSARY.md#l1-change) goes out at once, except a `clear` that a profile [holds](#holding-clear). What goes out for an L1 change is that sensor's entry in the newest result, so an L1 change a later one has already replaced never goes out, and a sensor with more than one L1 change before the publisher looks sends that entry once. A profile with `repeat = 0` does not repeat, and sends an L1 change or a clean stop's `unknown` only if its payload differs from the last one sent for that sensor. Sensors and the archive's `id` come from the newest result each time, so a reload needs no restart, and a sensor the manifest no longer names stops being reported. It says nothing more, and neither does a topic that a new archive `id` moves, so on a retained profile the broker keeps the last message on that topic. Until the first result every sensor is `unknown`, as [L1](#l1) says, and a repeat is never older than `scoring stalled` allows. A clean stop publishes `unknown` with the reason `service stopped` for every sensor, then disconnects. A lost broker is retried with no restart. Nothing goes out while a connection is down, and a message at `qos` 1 or 2 the broker had not acknowledged when it was lost is dropped, so the new connection sends only what is current.

An error in the publisher's own loop, such as a connection that cannot be made, is logged once. Then the connection disconnects cleanly, and nothing more is published. The process goes on, with `latest` and `events`, and a restart publishes again.

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
| `l0_topic` | Optional. The topic of [L0](#l0), which may hold `{archive_id}`. Absent means no L0. |
| `railroad_topic`, `railroad_field`, `railroad_stamp` | Optional, all three or none. A topic whose JSON object names the loaded railroad, which fills `{railroad}` in `--archive`, and the fields of its name and of its build's stamp. |

In a table, a string that is only `{confidence}` becomes a JSON number. Anywhere else a placeholder becomes text.

A placeholder is a name of letters, digits and `_` between braces. Braces around a name with a space or a hyphen, such as `{ sensor }` or `{archive-id}`, would go out as text, so a profile holding them in a topic, a payload or a table's key is refused. A table's key cannot hold a placeholder either, since a key is not filled. Other braces are text, so a string payload may be JSON text such as `'{"state": "on"}'`.

A `topic` or `l0_topic` that starts with `$`, which a broker keeps for its own topics, is refused, and so is one whose text without its placeholders is over 65535 bytes in UTF-8, the most MQTT allows.

A profile with an `l0_topic` publishes one message per detector run there, never retained, at the profile's `qos`. The message is the [`l0` object of `latest`](LIVE.md#l0) without its `frame_id`, which only `latest` can give. L0 does not repeat, and does not go out on a connect.

A sensor id or archive `id` that a topic holding it cannot carry, because it has `+`, `#`, `/`, a control character or a Unicode non-character, or makes a topic MQTT does not allow, is logged once, and again if a reload removes the sensor and adds it back, and that sensor is not published on that profile. The other sensors and profiles keep reporting.

### When the service dies or hangs

The connection has no last will. MQTT allows one will per connection, so a will on each sensor's topic would need a connection per sensor, and a will never covered a hung service whose connection stays up. A service that is killed, whose host or network fails, that hangs, or whose publisher fails, stops sending, and the broker sends nothing for it. What each controller sees then:

- `control`: its timer per sensor reads `unknown` after 3 s with no report.
- [JMRI](#jmri) keeps the last state. A JMRI-side script can do what `control` does: listen on the sensor topics itself, and set a sensor `ACTIVE` after 3 s with no message. It has to read the MQTT messages, as a repeat of the same payload changes no JMRI sensor's state. The `jmri` profile repeats every second, which such a script relies on. This repository does not ship one.
- [Rocrail](#rocrail) keeps the last state. Its profile does not repeat, so a timer on that side cannot work.

### Holding `clear`

One frame the classifier gets wrong would put `clear` on the topic in the middle of a train. Control takes the first `clear` after `occupied` on an entry sensor for the train's tail, so a profile can hold `clear` back.

The hold lowers how often a sensor reads `clear` in the middle of a train and does not rule it out: a `clear` lasting longer than the hold is still published.

With `clear_hold_s` set, a sensor whose last reported state on that profile is not `clear` publishes `clear` only once every result has read `clear` for that long. The time runs from the capture time of the first `clear` frame, and any other result restarts it, including one that came while the broker was gone. `clear` goes out on the first result captured at or after the end of the hold, so with the frames stopped it does not go out: a sensor goes `unknown` first.

- Until then the profile keeps reporting the last state it published, with its `reason` if it has one, on each repeat and each connect. A sensor that was `unknown` stays `unknown`, so a wrong `clear` right after an outage is held too.
- `occupied` and `unknown` go out at once, as do `unknown` at startup and on a clean stop.
- A hold ends only once its `clear` has gone out. Where the connection drops just as the hold ends, the reconnect sends the state held, and `clear` follows on the next pass.
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

`control`, whose contract is rails49/control#1147. `layout` has a timer per sensor, which reports a [dead or hung service](#when-the-service-dies-or-hangs) and wants every sensor at least once a second, so `repeat` leaves room for one to be late. It holds `clear` for 1 s, which rails49/occupancy-service#18 measures on real trains. It reads the loaded railroad from `tc49/layout/state/railroad` (`control/docs/BUS.md`):

```toml
topic = "tc49/layout/sensor"
retain = false
qos = 0
repeat = 0.9
clear_hold_s = 1
railroad_topic = "tc49/layout/state/railroad"
railroad_field = "name"
railroad_stamp = "at"

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
- A clean stop leaves every sensor `ACTIVE`, retained. A sensor a reload removes keeps the last state the broker retained for it.

JMRI does not notice a dead or hung service: its sensors keep the last state the service sent, unless a script on the JMRI side [times them out](#when-the-service-dies-or-hangs).

`rocrail`, for Rocrail's sensors. It is not active by default:

```toml
topic = "rocrail/service/field"
retain = false
qos = 1
repeat = 0

[payload]
occupied = '<fb id="{sensor}" state="true"/>'
clear = '<fb id="{sensor}" state="false"/>'
unknown = '<fb id="{sensor}" state="true"/>'
```

### Rocrail

Turn it on with `--profile rocrail`, or with `--profile occupancy --profile rocrail` to keep the own vocabulary too. In Rocrail, set the MQTT broker's host and port to the service's broker, as the [wiki's services page](https://wiki.rocrail.net/doku.php?id=rocrailini-service-en) says. A sensor's `id` must be Rocrail's sensor ID, and should avoid `"`, `<` and `&`: the service does not escape it, and Rocrail drops a message it cannot parse. `rocrail/service/field` needs no support key and no address or interface on the sensor. This was checked on Rocrail 26.05.15.

- `occupied` is `true` and `clear` is `false`. `unknown` is `true` too, as Rocrail has only the two: a sensor the service cannot see is taken as occupied.
- Nothing is retained and nothing repeats, because Rocrail takes a repeated `true` as a new event. A change goes out only when it turns `true` into `false` or back, so `occupied` to `unknown`, or a new reason, sends nothing.
- A clean stop turns every sensor `true`.
- A sensor the manifest removes is sent nothing more, and keeps its last state in Rocrail.

In automatic mode, `unknown`, or a false detection, on a block with no train is a ghost train, and by default Rocrail then turns off track power until a person turns it on. That includes a block just released while its sensor stays `true`, as a train's tail leaves it. With `poweroffatghost="false"` on `ctrl` in `rocrail.ini`, a ghost closes only that block, and a running train continues. Outside automatic mode no ghost is reported.

- Start the service before turning on automatic mode, so every sensor has its state first.
- Restart the service after restarting Rocrail. With no repeat and no retain, Rocrail keeps the states it had until the next change. This is expected, and not yet tested.

Rocrail does not notice a [dead or hung service](#when-the-service-dies-or-hangs), nor a lost broker: its sensors keep the last state the service sent.

## Box

`compose.yaml` runs the service and the [occupancy page](https://github.com/rails49/occupancy) behind the box's door. Both containers join the installation's `rails49` network and publish no ports. On a box with a box name, `compose.https.yaml` is loaded beside it: `occupancy.$BOX_DOMAIN` goes to the page over HTTPS, and `/live` on that host goes to the service, with the prefix kept. The page talks to the service on its own origin.

The door serves both on `occupancy`'s [UI port](https://github.com/rails49/installation#two-kinds-of-box), 8084, over plain HTTP, at any host. On a box with no certificate (`BOX_DOMAIN=`) that is the only way in: `http://<hostname>.local:8084` or `http://<address>:8084`. A box with a box name serves the page on 8084 too.

The box's settings for this stack are in `/etc/rails49/occupancy.env`, beside `box.env`. It holds no secret, and is written by hand:

```sh
TC49_UID=1000
TC49_GID=1000
VIDEO_GID=44
RENDER_GID=992
OCCUPANCY_ARCHIVES=/var/lib/rails49/tc49/occupancy
OCCUPANCY_CAMERA=/dev/v4l/by-id/usb-<camera>-video-index0
```

The stack is started with both files:

```sh
docker compose --env-file /etc/rails49/box.env --env-file /etc/rails49/occupancy.env \
  -f compose.yaml -f compose.https.yaml up -d
```

On a box with no box name, `-f compose.https.yaml` is left out.

The installation runs first: it creates the network and runs `broker`, which the service dials on port 1883. Then, from a clone of this repository on another machine:

```sh
scripts/deploy.sh <user>@<box>
```

`scripts/deploy.sh` goes over ssh to the clone at `~/occupancy-service` on the box, and stops on a box with no `occupancy.env`. It brings the clone to the head of `main` (or of the branch given after the box), and refuses a clone that is not clean. It builds both images there and starts them with both settings files and `compose.https.yaml`.

| Setting | Holds | Set in | Default |
| :--- | :--- | :--- | :--- |
| `BOX_DOMAIN` | The box name; empty on a box with no certificate | `box.env` | None |
| `OCCUPANCY_ARCHIVES` | The folder of archives on the box, one `<railroad>.r49` per railroad, mounted read-write at `/archives` | `occupancy.env` | None |
| `OCCUPANCY_CAMERA` | The camera: its path under `/dev/v4l/by-id/`, or an MJPEG stream URL ([below](#a-camera-that-is-not-a-usb-webcam)) | `occupancy.env` | None |
| `TC49_UID`, `TC49_GID` | The user and group the service runs as, so the files it saves are the person's (`id -u`, `id -g`) | `occupancy.env` | None |
| `VIDEO_GID`, `RENDER_GID` | The host's `video` and `render` groups, for the camera and the GPU (`getent group video`, `getent group render`) | `occupancy.env` | None |
| `OCCUPANCY_SERVICE_COMMIT` | The commit the images are named by and carry as `org.opencontainers.image.revision` | `scripts/deploy.sh` | `dev`, and an empty revision |

Compose names a missing setting and the file it belongs in, and starts nothing.

The images are `ghcr.io/rails49/occupancy-service:<commit>` and `ghcr.io/rails49/occupancy-page:<commit>`, for amd64 and arm64. The `images` workflow builds and publishes them when an [installation release](https://github.com/rails49/.github/blob/main/GLOSSARY.md) is made, and a box runs them from one. `scripts/deploy.sh` builds them on the box under the same names.

The service is passed `--archive=/archives/{railroad}.r49`, so it opens the archive of the railroad `control` has loaded, and runs with none for a railroad that has none yet. A page creates that railroad's archive in the folder with `PUT /live/archive.r49`. On a box with `control` the folder is `<store>/occupancy`, inside `control`'s store, so the store's backup carries the archives. The folder must exist: compose does not create it.

The service runs the `occupancy` and `control` profiles, pauses while control's track power is off, with OpenVINO on the GPU when the GPU is there, and on the CPU otherwise. It restarts unless stopped, so an archive that could not be read is picked up again once it is repaired. The host's `/dev` is mounted, with video devices and the GPU allowed, so a camera that is unplugged and plugged back in appears with no restart.

The page can save only to an archive that is a folder named `*.r49`. A zip is served, and refuses saves. To make a folder from a zip:

```sh
mv layout.r49 layout.zip && unzip layout.zip -d layout.r49
```

The page is built from the `rails49/occupancy` commit in `OCCUPANCY_COMMIT` in `compose.yaml`, and nginx sets the headers its `_headers` names. Moving to a new commit is a PR that changes that line.

The service image holds `rails49-detector` (AGPL) at the version `DETECTOR_VERSION` pins in `deploy/service.Dockerfile`, so a commit fixes it. The license text is at `/usr/share/doc/rails49-detector/LICENSE` in the image, and the URL of that version's source in `SOURCE` beside it. The detector stays off until the service's `command` has `--detector-model`, which rails49/occupancy-service#13 sets. The image holds no model.

With no camera at `OCCUPANCY_CAMERA`, the service keeps running: `latest` has `camera` `lost`, and every sensor reads `no frames`. `deploy/check.r49` is a small archive, with two sensors and no images, that the installation release's check starts the service with, with no camera.

Two steps belong to the box and are not code here:

- On a box with a box name, an `A` record for the `occupancy` label. The door gets a certificate per router, and holds no wildcard.
- `occupancy` in `BOX_UIS` in `/etc/rails49/box.env`, so the installation's page links the UI.

## A camera that is not a USB webcam

A camera that is not a USB webcam is read as an MJPEG stream, served by a program on the box's host or by the camera itself. `OCCUPANCY_CAMERA` is then the stream's URL, over `rtsp://` or `http://`. `ffmpeg` copies the stream's JPEGs as it copies a webcam's, and reads RTSP over TCP. The service's container reaches the box's own host as `host.docker.internal`.

- Only MJPEG is read. A stream in H.264 or any other codec gives no frames: `ffmpeg` fails and is started again, and every sensor reads `no frames`.
- Frames are not scaled. The stream's size must be the archive's `camera.resolution`; at any other size each sensor reads `wrong frame size`.
- A stream that ends or stalls is handled as an unplugged webcam: the camera is `lost`, and `ffmpeg` is started again until the stream is back.
- The service sets no camera controls. Exposure, white balance and focus are set where the stream is made: in the program that serves it, or in the camera's own settings.
- A user and password in the URL are passed to `ffmpeg` as given, and show in `ps` and in `ffmpeg`'s error lines.
- `capture_ms` and a frame's age count from when the frame reached the service. Time in the program that serves the stream, or on the network, is not in them.

### The Raspberry Pi camera with mediamtx

[mediamtx](https://github.com/bluenviron/mediamtx) serves the Pi's ribbon camera over RTSP. Download its `arm64` release onto the Pi, and replace the `paths` section of `mediamtx.yml` with:

```yml
paths:
  cam:
    source: rpiCamera
    rpiCameraCodec: mjpeg
    rpiCameraWidth: 1920   # the archive's camera.resolution
    rpiCameraHeight: 1080
```

`rpiCameraCodec` must be `mjpeg`: its default, `auto`, picks H.264, which the service does not read. Then, in `/etc/rails49/occupancy.env`:

```sh
OCCUPANCY_CAMERA=rtsp://host.docker.internal:8554/cam
```

To keep mediamtx running across a reboot, install it as a systemd service:

```sh
sudo mkdir -p /usr/local/bin /usr/local/etc/mediamtx
sudo mv mediamtx /usr/local/bin/
sudo mv mediamtx.yml /usr/local/etc/mediamtx/
sudo tee /etc/systemd/system/mediamtx.service >/dev/null <<EOF
[Unit]
After=network-online.target
Wants=network-online.target
[Service]
WorkingDirectory=/usr/local/etc/mediamtx
ExecStart=/usr/local/bin/mediamtx
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
```

With its default settings, mediamtx serves the stream to anyone on the LAN. Its `authInternalUsers` setting limits who may read it.

## Test

```sh
bin/test.sh             # Ruff, Black, Pyright and pytest
```

`tests/test_compose.py` runs `docker compose config` and reads the project it resolves, which needs the docker CLI and no daemon. `tests/test_deploy.py` runs `scripts/deploy.sh` with a fake `ssh` that runs the box's half on this machine, and is skipped where `/etc/rails49/occupancy.env` exists. `tests/test_images.py` runs against built images, and only when `OCCUPANCY_SERVICE_IMAGE` and `OCCUPANCY_PAGE_IMAGE` name them, with `OCCUPANCY_SERVICE_COMMIT` for the revision. Its check with no camera waits 60 seconds.

The `images` workflow (`.github/workflows/images.yml`) takes a commit. The installation's release workflow calls it, and it can be run by hand. For each image not yet on GHCR at that commit, it builds the image on an amd64 and an arm64 runner, runs `tests/test_images.py` against it, pushes it by digest, and joins the two under `:<commit>`. An image already there is not built again. It outputs the full commit as `commit` and both digests, as `occupancy-service` and `occupancy-page`. The caller grants `packages: write`. It uploads `compose.yaml`, `compose.https.yaml` and `deploy/check.r49` as the artifact `occupancy-service-box-files`. The service image installs the Intel compute runtime only on amd64.

The L1 tests use a fake capture source and the packaged model, on the CPU. The detector's tests use a fake detector, and the tests of the command a fake `detector` module on the child's path, so no AGPL package is installed. The drift tests run the [shared cases](LIVE.md#drift-cases), and the real check on the real loop. The server tests run real HTTP and `live-check` against the real loop, on the same fake source. The capture tests use a fake `ffmpeg`, so they need no camera. The tests of the real `ffmpeg` run its command line on a file, and on MJPEG and PNG streams from a local HTTP server, and are skipped when `ffmpeg` is not on the `PATH`. The MQTT tests of what goes out on the wire run a real `mosquitto`, which they start themselves on a free port with `rails49-testing`'s `Broker`. They are skipped when it is not installed, except under `CI`, where they fail. The tests of what the publisher decides run on a test connection, with no broker. CI installs both. The tests of the command run it as a real child process, with the fake `ffmpeg` on the `PATH`.
