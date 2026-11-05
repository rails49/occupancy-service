# The live contract

How a program reads live data from an occupancy service, over HTTP. Anyone can implement it. [`live-check`](#conformance) tests an implementation against it.

[L0](https://github.com/rails49/detector/blob/main/GLOSSARY.md) means what `detector`'s GLOSSARY says, and [L1](https://github.com/rails49/occupancy/blob/main/GLOSSARY.md) what `occupancy`'s GLOSSARY says. Sensor, archive and `camera.resolution` are [r49's](https://github.com/rails49/r49/blob/main/FORMAT.md). A frame is one picture from the live camera.

Nothing here is a safety interlock.

## Rules for every part

- Everything sits under a base URL, written `<base>`.
- All coordinates are `camera.resolution` pixels.
- A reader ignores keys it does not know.
- An absent optional field means the service does not offer it. `patches` is the exception: see [Optional fields](#optional-fields).
- JSON is UTF-8. It holds no `NaN` or `Infinity`.

| URL | Required | Holds |
| :--- | :--- | :--- |
| `GET <base>/latest` | yes | The latest frame and its L1 |
| `GET <base>/events` | yes | Every L1 change |
| `<base>/archive/` | yes | The archive |
| `PUT` and `DELETE` under `<base>/archive/` | no | [Saving](#saving) back to the archive |

MQTT is not in the contract. The [README](README.md#mqtt) documents the profiles.

## `GET <base>/latest`

The answer is `multipart/form-data`, with everything from one frame.

| Part | Required | Holds |
| :--- | :--- | :--- |
| `json` | yes | The fields below, with no `filename`, so a reader gets it as a string |
| `frame` | when there is a frame | The captured JPEG at `camera.resolution`, with a `filename` |
| `patch-0`, `patch-1`, ... | with `?patches=1`, when offered | One image per sensor's patch, each with a `filename`, and its type in `Content-Type` |

A binary part needs its `filename` because of `response.formData()`: without one, Chromium 156 rejects the whole answer and WebKit 27.2 returns an empty string.

### Query parameters

- `?patches=1` asks for the patch parts. A service that does not offer patches ignores it.
- `?after=<frame id>` waits up to about 1 s for a frame newer than that one, then answers with what it has.

They work together: `latest?after=<frame id>&patches=1`.

### When there is no frame

With no frame in about 1 s, the answer is only the JSON part. Every sensor is `unknown` with its reason, and `frame_id` and `captured_at` are `null`.

A frame part can also come with every sensor `unknown`, for example when the camera has moved.

### The JSON part

| Field | Type | Meaning |
| :--- | :--- | :--- |
| `version` | the integer `1` | The version of this contract. |
| `frame_id` | non-empty string, or `null` with no frame | The frame's id. Opaque to a reader, which only compares it and sends it back in `after`. |
| `captured_at` | RFC 3339 time with an offset, or `null` with no frame | When the frame reached the service, so USB delay is not in it. |
| `l1` | object | Sensor id to [L1 entry](#l1-entry), with one entry per sensor in the archive's manifest and no others. |

### L1 entry

| `state` | Other fields |
| :--- | :--- |
| `occupied` | `confidence`, a number from 0 to 1 |
| `clear` | No `confidence` field |
| `unknown` | `reason`, non-empty free text, and no `confidence` field |

A classifier's number for a clear sensor goes in the optional `scores`.

### Optional fields

Everything except `l0`, `drift` and `last_messages` comes from the served frame.

| Field | Type | Meaning |
| :--- | :--- | :--- |
| `l0` | object | The newest L0. It has its own `frame_id` and `captured_at`, because the detector runs slower than the camera. rails49/occupancy-service#8 defines the rest of it. |
| `scores` | object | Sensor id to P(occupied), from 0 to 1. |
| `threshold` | number from 0 to 1 | The number a score is compared with. |
| `patches` | object | Sensor id to the name of a part in this answer, such as `patch-0`, a different part for each sensor. A sensor with no patch has no entry. Sent only in answer to `?patches=1`. |
| `changes` | object | Sensor id to the count of L1 changes, an integer from 0. |
| `timings` | object | Any of `capture_ms`, `decode_ms`, `cut_ms`, `classify_ms` and `rate_fps`, each a number from 0. |
| `camera` | object | Any of `state` (`ok` or `lost`), `rate_fps` and `frame_age_ms`, the last two numbers from 0. |
| `drift` | object | How far the camera has moved, from its own sampled frame. See [Drift](#drift). |
| `last_messages` | object | Sensor id to profile name to `{ message, time }`: the last message that profile published for that sensor, and when. A sensor or a profile with nothing published has no entry. `message` is an object for a payload sent as JSON and a string for one sent as text. `time` has the form of `captured_at`. |

`patches` maps ids to part names because a sensor id is any non-empty string, and a part name cannot carry one safely: browsers return a `"` written as `%22` as those three characters.

### Example

`GET <base>/latest?after=f-1041&patches=1` on an archive with the sensors `C3.A` and `C3.B`, at 1920 by 1080. The service offers every optional field:

```http
HTTP/1.1 200 OK
Content-Type: multipart/form-data; boundary=r49-live

--r49-live
Content-Disposition: form-data; name="json"
Content-Type: application/json

{
  "version": 1,
  "frame_id": "f-1042",
  "captured_at": "2026-11-05T10:15:30.125Z",
  "l1": {
    "C3.A": { "state": "occupied", "confidence": 0.93 },
    "C3.B": { "state": "clear" }
  },
  "l0": { "frame_id": "f-1040", "captured_at": "2026-11-05T10:15:29.991Z" },
  "scores": { "C3.A": 0.93, "C3.B": 0.02 },
  "threshold": 0.5,
  "patches": { "C3.A": "patch-0", "C3.B": "patch-1" },
  "changes": { "C3.A": 4, "C3.B": 3 },
  "timings": { "capture_ms": 2.5, "decode_ms": 11, "cut_ms": 1.25, "classify_ms": 30, "rate_fps": 14.5 },
  "camera": { "state": "ok", "rate_fps": 15, "frame_age_ms": 20 },
  "drift": { "px": 2, "tolerance_px": 9.75, "past": false },
  "last_messages": {
    "C3.A": {
      "occupancy": { "message": { "state": "occupied", "confidence": 0.93 }, "time": "2026-11-05T10:15:30.140Z" }
    },
    "C3.B": {
      "occupancy": { "message": { "state": "clear" }, "time": "2026-11-05T10:15:30.141Z" }
    }
  }
}
--r49-live
Content-Disposition: form-data; name="frame"; filename="frame.jpg"
Content-Type: image/jpeg

<JPEG bytes>
--r49-live
Content-Disposition: form-data; name="patch-0"; filename="patch-0.jpg"
Content-Type: image/jpeg

<JPEG bytes>
--r49-live
Content-Disposition: form-data; name="patch-1"; filename="patch-1.jpg"
Content-Type: image/jpeg

<JPEG bytes>
--r49-live--
```

The JSON part with no frame in about 1 s:

```json
{
  "version": 1,
  "frame_id": null,
  "captured_at": null,
  "l1": {
    "C3.A": { "state": "unknown", "reason": "no frames" },
    "C3.B": { "state": "unknown", "reason": "no frames" }
  }
}
```

### Drift

`drift` has one of two forms:

| Field | Type | Meaning |
| :--- | :--- | :--- |
| `px` | number from 0 | How far the camera has moved since the archive's `images[0]`, in pixels of that image, not `camera.resolution`. |
| `tolerance_px` | number from 0 | The largest reading accepted: a setting in track gauges, multiplied by the layout's DPT. |
| `past` | boolean | Whether `px` is above `tolerance_px`. Exactly at it is not past it. |

| Field | Type | Meaning |
| :--- | :--- | :--- |
| `unmeasured` | non-empty string | Why there is no reading. |

While `past` is true, every sensor is `unknown` with the reason `drift`. One reading past the tolerance refuses and one within it ends the refusal, with nothing filtered across readings. With `unmeasured`, L1 goes on as if the camera had not moved. A service with the check turned off has no `drift` field and never refuses.

A reading is the median over blocks of block-wise phase correlation. Both images go to Rec. 601 luma, stretched bilinearly to a working width of at most 960 px, and are cut into 128 px blocks, fewer only if the image is too small. Each block gets a Hann window, and its lag is the highest whole-pixel peak of the whitened cross-power spectrum, so a camera that has not moved reads exactly 0.

#### Drift cases

[`conformance/drift.json`](conformance/drift.json) holds image pairs and the reading each should give, for an implementation to run in its own tests:

| Key | Holds |
| :--- | :--- |
| `width`, `height` | The size of every image. |
| `images` | Image name to its pixels in base64: 8-bit gray, row by row, one byte per pixel. A value `v` is the luma `v / 255`. |
| `cases` | A list of `{ name, reference, frame, expected_px }`, where `reference` and `frame` are names in `images`. |

An implementation passes a case when it reads `expected_px` within 0.000001, with `reference` as `images[0]`. `conformance/make_drift.py` wrote the file.

## `GET <base>/events`

Server-Sent Events: every sensor's state on connect, then each change. Each event is one sensor's [L1 entry](#l1-entry) with its id in `sensor`, as a message with no `event` field, so `EventSource.onmessage` sees it.

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream

: every sensor's state on connect
data: {"sensor": "C3.A", "state": "occupied", "confidence": 0.93}

data: {"sensor": "C3.B", "state": "clear"}

: then each change
data: {"sensor": "C3.A", "state": "clear"}

data: {"sensor": "C3.B", "state": "unknown", "reason": "no frames"}

```

## `<base>/archive/`

Serves the archive in the URL form of rails49/r49#60: `<base>/archive/manifest.json` and `<base>/archive/<filename>`. `manifest.json` has the content type `application/json`.

## Saving

Saving back to the archive is optional. A service offers it when the archive is a folder it can write to. `GET <base>/archive/manifest.json` then carries a strong `ETag`: the SHA-256 of the file's bytes, in hex, in double quotes.

With no `ETag`, as with a zip or a read-only folder, `PUT` and `DELETE` answer 405 with `Allow: GET`.

A page writes the images, then the manifest, then deletes the images it removed. It sends the `ETag` it read in `If-Match` each time, and takes the new one from the manifest's answer.

### Every write

Under `<base>/archive/<path>`, checked in this order:

| Status | When |
| :--- | :--- |
| 403 | The `Origin` is foreign, by the rule in [control's ADR-0057](https://github.com/rails49/control/blob/main/docs/adr/0057-one-origin-rule-and-both-faces-read-it.md). No `Origin` passes. |
| 428 | There is no `If-Match`. |
| 412 | `If-Match` is anything but the current manifest's `ETag`. |
| 404 | `manifest/filename` refuses the name, or a segment starts with a dot. Names are percent-decoded as on `GET`. |
| 411 | A `PUT` has no `Content-Length`. |

### Routes

| Route | Answer |
| :--- | :--- |
| `PUT <filename>` | 204. Stores the image, creating folders as needed, and replaces a file already there. 409 if a folder is at that name, or a file on the way to it. |
| `PUT manifest.json` | 204 with the new `ETag`. 422 with the rule in a JSON `error` field if it is refused, as below. |
| `DELETE <filename>` | 204. 409 if the manifest still names the file, or the name is `manifest.json`. 404 if there is no such file. |

| `error` | A manifest is refused because |
| :--- | :--- |
| A rule of [r49's FORMAT.md](https://github.com/rails49/r49/blob/main/FORMAT.md) | `parse_manifest` refuses it, under that rule. |
| `images/missing` | It names an image that is absent. |
| `manifest/id` | It changes or removes an `id` the archive has. |

### After a save

- Of two saves from one copy, one succeeds and the other gets 412.
- A failed write leaves the old file in place. A crash or a refused save may leave an image no manifest names, which a reader reports as an orphan, and which does no harm.
- The next frame scored uses the new manifest. A sensor that stays gets no `unknown` from the save.
- `events` clients are closed at the first result under the new manifest. A reader treats a connect as the full list of sensors.
- The service does not watch the folder. A hand edit takes effect at restart, and a page holding the earlier copy is refused.

## Conformance

One command checks a running implementation against this document:

```sh
uvx --from git+https://github.com/rails49/occupancy-service live-check <base>
```

It exits non-zero and names each broken rule. It accepts an implementation that omits every optional field, and checks an optional field's shape when present. If it cannot read the manifest it stops there, because the other rules need it.

With `--save` it also checks saving: 428, 412, 403 and 422, and that a rewrite of the same bytes succeeds. With no `ETag` it checks for 405. It tries each of these on `manifest.json`, so run it with care on an archive that matters: a service that wrongly accepts the refused manifest has it overwritten, and then put back. Without the flag it writes nothing.

It checks format and consistency only, so it does not run the [drift cases](#drift-cases). Timing rules need control of a camera, so they stay in rails49/occupancy-service#2 and rails49/occupancy-service#5. It checks the answer the service gives at that moment, so run it with the camera on and off to cover both forms of `latest`.

| Rule | Broken by |
| :--- | :--- |
| `http/unreachable` | A required URL does not answer. |
| `archive/status` | `manifest.json` does not answer 200. |
| `archive/content-type` | `manifest.json` is not `application/json`. |
| `archive/manifest` | The published `rails49-r49` refuses `manifest.json`. |
| `latest/status` | `latest` does not answer 200. |
| `latest/content-type` | The answer is not `multipart/form-data` with a boundary. |
| `latest/multipart` | The body has no closing delimiter. |
| `latest/json-part` | No part named `json`, or it has a `filename`, or it is not a JSON object. |
| `latest/version` | `version` is not the integer 1. |
| `latest/frame-id` | No non-empty `frame_id` with a frame, or anything but `null` with no frame. |
| `latest/captured-at` | No RFC 3339 `captured_at` with a frame, or anything but `null` with no frame. |
| `latest/no-frame-state` | No frame part while a sensor is not `unknown`. |
| `latest/no-frame-parts` | No frame part, and a part other than `json`. |
| `frame/no-filename` | The `frame` part has no `filename`. |
| `frame/jpeg` | The `frame` part does not decode as a JPEG. |
| `frame/size` | The frame is not at `camera.resolution`. |
| `l1/shape` | `l1` is not an object. |
| `l1/missing-sensor` | A sensor in the manifest has no entry. |
| `l1/extra-sensor` | An entry names a sensor not in the manifest. |
| `l1/state` | An entry has no `state` of `occupied`, `clear` or `unknown`. |
| `l1/occupied-confidence` | `occupied` with no `confidence` from 0 to 1. |
| `l1/clear-confidence` | `clear` with a `confidence`. |
| `l1/unknown-reason` | `unknown` with no non-empty `reason`. |
| `l1/unknown-confidence` | `unknown` with a `confidence`. |
| `optional/l0` | `l0` has the wrong shape. |
| `optional/scores` | `scores` has the wrong shape, or names a sensor not in the manifest. |
| `optional/threshold` | `threshold` has the wrong shape. |
| `optional/patches` | `patches` has the wrong shape, or names a sensor not in the manifest. |
| `optional/changes` | `changes` has the wrong shape, or names a sensor not in the manifest. |
| `optional/timings` | `timings` has the wrong shape. |
| `optional/camera` | `camera` has the wrong shape. |
| `optional/drift` | `drift` has the wrong shape, or `past` disagrees with `px` and `tolerance_px`. |
| `optional/last_messages` | `last_messages` has the wrong shape, or names a sensor not in the manifest. |
| `patches/missing-part` | `patches` names a part the answer does not have. |
| `patches/no-filename` | A part `patches` names has no `filename`. |
| `patches/after` | `?patches=1` gives `patches`, and with a frame `?after=<frame id>&patches=1` does not. |
| `events/status` | `events` does not answer 200. |
| `events/content-type` | The stream is not `text/event-stream`. |
| `events/data` | An event's data is not a JSON object with a non-empty `sensor`. |
| `events/extra-sensor` | An event names a sensor not in the manifest. |
| `events/missing-sensor` | A sensor in the manifest has no state on connecting. |
| `save/not-offered` | With `--save` and no `ETag`, a write does not answer 405 with `Allow: GET`. |
| `save/etag` | With `--save`, the `ETag` is not the SHA-256 of the manifest's bytes in double quotes. |
| `save/if-match-missing` | With `--save`, a save with no `If-Match` does not answer 428. |
| `save/if-match-stale` | With `--save`, a save with another `If-Match` does not answer 412. |
| `save/origin` | With `--save`, a save with a foreign `Origin` does not answer 403. |
| `save/manifest-refused` | With `--save`, a manifest with no version does not answer 422 with a JSON `error` field. |
| `save/rewrite` | With `--save`, a rewrite of the same bytes does not answer 204 with the same `ETag`. |

An event breaks the `l1/` rules as an entry in `latest` does.
