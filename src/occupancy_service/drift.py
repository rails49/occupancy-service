"""How far the camera has moved since the archive's first image.

A port of ``@occupancy/drift`` in rails49/occupancy, which ``conformance/drift.json`` keeps in step
with this one. Each block's lag is the peak of the whitened cross-power spectrum, so the lighting
drops out with the magnitude. A moved camera shifts every block and a moved car only some, so the
median over blocks reads the camera.
"""

import io
import math

import numpy as np
import numpy.typing as npt
from PIL import Image

type Gray = npt.NDArray[np.float32]
"""Height × width, row-major, from 0 to 1."""

_WORKING_WIDTH = 960
_BLOCK_SIZE = 128
_MIN_BLOCK_SIZE = 32
_NO_PHASE = 1e-12  # A bin weaker than this holds no phase to read.


def gray_of(image: bytes) -> Gray:
    """An encoded image in Rec. 601 luma. Raises what Pillow raises when it does not decode."""
    with Image.open(io.BytesIO(image)) as opened:
        rgb = np.asarray(opened.convert("RGB"), np.float64)
    luma = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    return (luma / 255).astype(np.float32)


def _resampled(plane: Gray, width: int, height: int) -> Gray:
    """``plane`` stretched to exactly this size: bilinear, sampling at pixel centres."""
    if plane.shape == (height, width):
        return plane
    rows, columns = plane.shape

    def sampled(
        count: int, source: int
    ) -> tuple[npt.NDArray[np.intp], npt.NDArray[np.intp], npt.NDArray[np.floating]]:
        at = np.clip((np.arange(count) + 0.5) * (source / count) - 0.5, 0, source - 1)
        low = np.floor(at).astype(np.intp)
        return low, np.minimum(low + 1, source - 1), at - low

    y0, y1, fy = sampled(height, rows)
    x0, x1, fx = sampled(width, columns)
    top = plane[y0][:, x0] * (1 - fx) + plane[y0][:, x1] * fx
    bottom = plane[y1][:, x0] * (1 - fx) + plane[y1][:, x1] * fx
    return (top * (1 - fy)[:, None] + bottom * fy[:, None]).astype(np.float32)


def _block_size(width: int, height: int) -> int:
    """128 px, stepping down for a plane too small for it."""
    size = _BLOCK_SIZE
    while size > min(width, height):
        size >>= 1
    if size < _MIN_BLOCK_SIZE:
        raise ValueError(f"a {width}x{height} image is too small to hold a block")
    return size


class DriftCheck:
    """Readings against one reference image. The reference's half of the work is done once."""

    def __init__(self, reference: Gray) -> None:
        """Raises ``ValueError`` for a reference too small to hold a block."""
        rows, columns = reference.shape
        self._width = min(_WORKING_WIDTH, columns)
        self._height = max(1, math.floor(rows * self._width / columns + 0.5))
        self._px_per_lag = columns / self._width
        self._size = _block_size(self._width, self._height)
        hann = (0.5 * (1 - np.cos(2 * np.pi * np.arange(self._size) / (self._size - 1)))).astype(
            np.float32
        )
        self._window = np.outer(hann, hann).astype(np.float32).astype(np.float64)
        self._reference = self._spectra(reference)

    def read(self, frame: Gray) -> float:
        """How far ``frame``, at any size, has moved, in pixels of the reference."""
        cross = self._spectra(frame).astype(np.complex128) * np.conj(
            self._reference.astype(np.complex128)
        )
        magnitude = np.abs(cross)
        whitened = np.divide(
            cross, magnitude, out=np.zeros_like(cross), where=magnitude >= _NO_PHASE
        )
        correlation = np.real(np.fft.ifft2(whitened)).reshape(len(cross), -1)
        # Whole-pixel lags, so a frame that did not move reads exactly 0. The transform wraps, so
        # a lag past halfway is a negative one.
        lag_y, lag_x = np.divmod(np.argmax(correlation, axis=1), self._size)
        half = self._size / 2
        dy = np.where(lag_y > half, lag_y - self._size, lag_y)
        dx = np.where(lag_x > half, lag_x - self._size, lag_x)
        return float(np.median(np.hypot(dx, dy))) * self._px_per_lag

    def _spectra(self, plane: Gray) -> npt.NDArray[np.complex64]:
        """The FFT of each whole block, windowed, with the leftover split between the edges."""
        size = self._size
        rows, columns = self._height // size, self._width // size
        top, left = (self._height - rows * size) // 2, (self._width - columns * size) // 2
        resampled = _resampled(plane, self._width, self._height)
        tiled = resampled[top : top + rows * size, left : left + columns * size]
        blocks = tiled.reshape(rows, size, columns, size).swapaxes(1, 2).reshape(-1, size, size)
        return np.fft.fft2(blocks.astype(np.float64) * self._window).astype(np.complex64)
