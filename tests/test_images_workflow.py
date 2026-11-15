"""`.github/workflows/images.yml`, read rather than run."""

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "images.yml"


def test_each_build_caches_its_layers_in_ghcr() -> None:
    """One cache per image and architecture, with every stage's layers, so a release rebuilds and
    a box pulls only the layers that changed (rails49/.github#80)."""
    lines = [line.strip() for line in WORKFLOW.read_text().splitlines()]
    ref = (
        "type=registry,ref=ghcr.io/rails49/occupancy-${{ matrix.role }}"
        ":buildcache-${{ runner.arch }}"
    )
    assert lines.count(f"cache-from: {ref}") == 1
    assert lines.count(f"cache-to: {ref},mode=max") == 1
