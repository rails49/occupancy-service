"""Write drift.json. Each expected reading is the shift this script applies, not a run of a check.

Run it with ``uv run python conformance/make_drift.py``.
"""

import base64
import json
from pathlib import Path

import numpy as np
import numpy.typing as npt

WIDTH, HEIGHT = 384, 128  # Three blocks of 128 px, so the median has a middle.
MARGIN = 64


def _scene() -> npt.NDArray[np.float64]:
    """A picture larger than any view of it, so that a view from elsewhere has no invented edge:
    low frequencies as in a photograph, and noise, so that each block has a peak."""
    rng = np.random.default_rng(49)
    y, x = np.mgrid[: HEIGHT + 2 * MARGIN, : WIDTH + 2 * MARGIN]
    smooth = 0.18 * np.sin(x * 0.11) + 0.14 * np.cos(y * 0.073 + x * 0.031)
    return 0.5 + smooth + 0.22 * (rng.random(x.shape) - 0.5)


SCENE = _scene()


def _view(dx: int = 0, dy: int = 0) -> npt.NDArray[np.float64]:
    return SCENE[MARGIN + dy : MARGIN + dy + HEIGHT, MARGIN + dx : MARGIN + dx + WIDTH]


def _encode(plane: npt.NDArray[np.float64]) -> str:
    raw = np.clip(np.rint(plane * 255), 0, 255).astype(np.uint8)
    return base64.b64encode(raw.tobytes()).decode()


def main() -> None:
    still = _view()
    one_block_moved = still.copy()
    one_block_moved[:, 128:256] = _view(dx=20)[:, 128:256]
    images = {
        "reference": still,
        "shifted-3-0": _view(dx=3),
        "shifted-4-3": _view(dx=4, dy=3),
        "one-block-moved": one_block_moved,
        "brighter": np.clip(still, 0, 1) ** 1.2 + 0.08,
    }
    cases = [
        ("no shift", "reference", 0),
        ("a whole-pixel shift", "shifted-3-0", 3),
        ("a whole-pixel shift on both axes", "shifted-4-3", 5),
        ("a scene change that moves one block of three", "one-block-moved", 0),
        ("a brightness change", "brighter", 0),
    ]
    document = {
        "width": WIDTH,
        "height": HEIGHT,
        "images": {name: _encode(plane) for name, plane in images.items()},
        "cases": [
            {"name": name, "reference": "reference", "frame": frame, "expected_px": px}
            for name, frame, px in cases
        ],
    }
    path = Path(__file__).with_name("drift.json")
    path.write_text(json.dumps(document, indent=1) + "\n")


if __name__ == "__main__":
    main()
