"""The drift check, on the shared cases and on what they do not cover."""

import base64
import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from occupancy_service.drift import DriftCheck, gray_of

_SHARED = json.loads((Path(__file__).parent.parent / "conformance" / "drift.json").read_text())


def _plane(name: str) -> Any:
    raw = base64.b64decode(_SHARED["images"][name])
    shape = (_SHARED["height"], _SHARED["width"])
    return (np.frombuffer(raw, np.uint8).reshape(shape) / 255).astype(np.float32)


def _noise(width: int, height: int, dx: int = 0) -> Any:
    """A view of one noise picture, from ``dx`` pixels to the right."""
    rng = np.random.default_rng(7)
    picture = rng.random((height, width + 64), np.float32)
    return picture[:, dx : dx + width]


@pytest.mark.parametrize("case", _SHARED["cases"], ids=lambda case: case["name"])
def test_the_shared_cases(case: dict[str, Any]) -> None:
    check = DriftCheck(_plane(case["reference"]))
    assert check.read(_plane(case["frame"])) == pytest.approx(case["expected_px"], abs=1e-6)


def test_a_reading_is_in_pixels_of_the_reference_and_not_of_the_working_width() -> None:
    check = DriftCheck(_noise(1920, 256))
    assert check.read(_noise(1920, 256)) == 0
    assert check.read(_noise(1920, 256, dx=10)) == pytest.approx(10, abs=1e-6)


def test_a_frame_of_another_size_is_stretched_onto_the_reference() -> None:
    check = DriftCheck(np.full((256, 256), 0.5, np.float32))
    assert check.read(np.full((128, 128), 0.5, np.float32)) == 0


def test_a_reference_too_small_to_hold_a_block_is_refused() -> None:
    with pytest.raises(ValueError, match="too small"):
        DriftCheck(np.zeros((16, 64), np.float32))


def test_gray_is_rec_601_luma_from_0_to_1() -> None:
    out = io.BytesIO()
    Image.fromarray(np.array([[[255, 0, 0], [0, 255, 0]]], np.uint8)).save(out, "PNG")
    assert gray_of(out.getvalue())[0].tolist() == pytest.approx([0.299, 0.587], abs=1e-6)
