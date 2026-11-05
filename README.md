# occupancy-service

A process that takes one archive and one camera, runs the models where the frames are, and publishes each sensor's occupancy. It reports what the models see, which is sometimes wrong. It is not a safety interlock.

[LIVE.md](LIVE.md) is the HTTP contract for its live data. Anyone can implement it.

## Install

```sh
uv sync
bin/install-hooks.sh    # refuse commits on main, and run the gate before each push
```

## Run

The service itself is not written yet. `live-check` tests a running implementation against the contract:

```sh
uv run live-check http://localhost:8000/occupancy
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

## Test

```sh
bin/test.sh             # Ruff, Black, Pyright and pytest
```

The L1 tests use a fake capture source and the packaged model, on the CPU. The capture tests use a fake `ffmpeg`, so they need no camera. One test runs the real `ffmpeg` command line, and is skipped when `ffmpeg` is not on the `PATH`.
