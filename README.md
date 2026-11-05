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

## Test

```sh
bin/test.sh             # Ruff, Black, Pyright and pytest
```

The capture tests use a fake `ffmpeg`, so they need no camera. One test runs the real `ffmpeg` command line, and is skipped when `ffmpeg` is not on the `PATH`.
