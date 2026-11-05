# occupancy-service

A process that takes one archive and one camera, runs the models where the frames are, and publishes each sensor's occupancy. It reports what the models see, which is sometimes wrong. It is not a safety interlock.

[LIVE.md](LIVE.md) is the HTTP contract for its live data. Anyone can implement it.

## Install

```sh
uv sync
bin/install-hooks.sh    # refuse commits on main, and run the gate before each push
```

## Run

The process that runs it all is not written yet: `Capture`, `L1Loop` and `LiveServer` are started by the caller. `live-check` tests a running implementation against the contract:

```sh
uv run live-check http://localhost:8000/live
```

## L1

`occupancy_service.l1` turns the newest frame into L1. It reads the archive's manifest and no image. `L1Settings` holds the archive's path, the distance from a track past which a sensor has no track (0.3 G), and the engine, device and model, which default to onnxruntime and the model in the `rails49-classifier` wheel.

At or above the model's threshold a sensor is `occupied`, and below it `clear`. There is no smoothing across frames. A sensor is `unknown`, with one of these reasons, the first that applies:

| Reason | When |
| :--- | :--- |
| `archive has no id` | The manifest has no `id`. The service keeps running, so that a page can save the archive and mint one. |
| `no model` | The model did not load. |
| `DPT unresolved` | The calibration gives no DPT. |
| `no track` | The sensor is farther than the limit from every track. |
| `no frames` | The camera is `lost`, which takes about 1 s without a frame. Nothing is served from before the loss. |
| `scoring stalled` | The camera is fine, and for about 1 s no result has come. |
| `wrong frame size` | The frame does not match `camera.resolution`. |
| `outside the frame` | The classifier returned `None` for the sensor. |

**The turnout limit:** a sensor near a turnout is scored like any other, but the training data leaves such positions out, so the classifier's accuracy there is not known. The service does not detect these sensors, and reports nothing special for them.

## HTTP

`occupancy_service.server` serves [LIVE.md](LIVE.md) under a fixed `/live`, with a thread per request. `ServerSettings` holds the address and port, which default to `127.0.0.1:8000`, and the limits of `events`. The caller owns the `L1Loop`. There are no `Access-Control-*` headers, and no check of `Origin`.

| Route | Answer |
| :--- | :--- |
| `GET /live/latest` | The newest scored frame, not the newest captured one. It does not wait: with no frame to serve it is the JSON part alone. |
| `GET /live/latest?after=<frame id>` | The same, once there is a newer frame, or after about 1 s. An id from before a restart counts as older than any frame. |
| `GET /live/latest?patches=1` | Also one PNG part per sensor with a patch, encoded only then. |
| `GET /live/events` | Every sensor's state, then each new state or reason, with a comment line every 15 s. |
| `GET /live/archive/<path>` | The file, through `rails49-r49`'s `image()`, from a zip or a folder. What it reports absent, or a path with a segment that starts with a dot, is a 404. |

`latest`, `events` and `manifest.json` are `no-store`. `frame_id` holds a token for this run of the service, and readers treat it as opaque.

A frame not at `camera.resolution` is left out of `latest`, because the contract has the frame part at that size, and each sensor says `wrong frame size`. With no frame served, `timings` holds only `rate_fps`.

An `events` client that takes over 2 s over one write, or has over 1000 changes waiting, is dropped, and gets the full state when it reconnects. Each client waits on its own thread, so it holds up neither the others nor the scoring loop.

## Test

```sh
bin/test.sh             # Ruff, Black, Pyright and pytest
```

The L1 tests use a fake capture source and the packaged model, on the CPU. The server tests run real HTTP and `live-check` against the real loop, on the same fake source. The capture tests use a fake `ffmpeg`, so they need no camera. One test runs the real `ffmpeg` command line, and is skipped when `ffmpeg` is not on the `PATH`.
