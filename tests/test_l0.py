"""The detector's boxes against the shared cases in conformance/l1.json."""

import json
from pathlib import Path
from typing import Any

import pytest
from r49.data import STANDARD_GAUGE

from occupancy_service.l0 import car_width, detection_from_json, detector_says

CASES = json.loads((Path(__file__).parent.parent / "conformance" / "l1.json").read_text())


def entry(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each sensor's state, and its confidence when occupied, as the case's ``expected`` has it."""
    says = detector_says(
        [detection_from_json(d) for d in case["detections"]],
        {id: (s["x"], s["y"]) for id, s in case["sensors"].items()},
        (case["width"], case["height"]),
        case["car_width"],
        case["margin_g"] * case["dpt"],
    )
    found: dict[str, dict[str, Any]] = {}
    for id in case["sensors"]:
        said = says.get(id)
        if said is None:
            found[id] = {"state": "unknown"}
        elif said.occupied:
            found[id] = {"state": "occupied", "confidence": said.confidence}
        else:
            found[id] = {"state": "clear"}
    return found


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_a_shared_case(case: dict[str, Any]) -> None:
    assert entry(case) == case["expected"]


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_a_cases_car_width_is_the_dpt_times_standard_width_over_standard_gauge(
    case: dict[str, Any],
) -> None:
    assert CASES["standard_gauge_mm"] == STANDARD_GAUGE
    expected = case["dpt"] * CASES["standard_width_mm"] / CASES["standard_gauge_mm"]
    assert case["car_width"] == pytest.approx(expected)
    assert car_width(case["dpt"]) == pytest.approx(expected)


def test_the_cases_cover_what_the_brief_lists() -> None:
    names = " ".join(case["name"] for case in CASES["cases"])
    for covered in ("edge", "car width", "highest confidence", "both ends", "outside the frame"):
        assert covered in names
    assert all(case["margin_g"] == 2 for case in CASES["cases"])
