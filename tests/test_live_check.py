"""The checker against a stub server: LIVE.md's rules, one broken at a time."""

import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from stub import (
    HEIGHT,
    SENSORS,
    WIDTH,
    Part,
    Stub,
    answer,
    events,
    jpeg,
    manifest,
    no_frame_answer,
    serving,
)

from occupancy_service.live_check import check, main

LIVE_MD = (Path(__file__).parent.parent / "LIVE.md").read_text()


def failures(stub: Stub) -> list[str]:
    """The rules ``stub`` breaks. Each one must be a rule LIVE.md lists."""
    with serving(stub) as base:
        rules = rules_at(base, timeout=2)
    for rule in rules:
        assert f"`{rule}`" in LIVE_MD
    return rules


def rules_at(base: str, timeout: float) -> list[str]:
    return [failure.rule for failure in check(base, timeout=timeout)]


def latest_with(**changes: Any) -> dict[str, Any]:
    return answer() | changes


def l1_with(entry: Any) -> dict[str, Any]:
    return latest_with(l1=answer()["l1"] | {"C3.A": entry})


def test_accepts_a_service_with_no_optional_field() -> None:
    assert failures(Stub()) == []


@pytest.mark.parametrize(
    ("entry", "rule"),
    [
        ({"state": "clear", "confidence": 0.1}, "l1/clear-confidence"),
        ({"state": "unknown"}, "l1/unknown-reason"),
        ({"state": "unknown", "reason": ""}, "l1/unknown-reason"),
        ({"state": "unknown", "reason": 7}, "l1/unknown-reason"),
        ({"state": "unknown", "reason": "drift", "confidence": 0.5}, "l1/unknown-confidence"),
        ({"state": "occupied"}, "l1/occupied-confidence"),
        ({"state": "occupied", "confidence": 1.5}, "l1/occupied-confidence"),
        ({"state": "occupied", "confidence": True}, "l1/occupied-confidence"),
        ({"state": "maybe"}, "l1/state"),
        ("clear", "l1/state"),
    ],
)
def test_refuses_an_l1_entry(entry: Any, rule: str) -> None:
    assert failures(Stub(latest=l1_with(entry))) == [rule]


def test_refuses_a_missing_sensor() -> None:
    assert failures(Stub(latest=latest_with(l1={"C3.A": {"state": "clear"}}))) == [
        "l1/missing-sensor"
    ]


def test_refuses_a_sensor_not_in_the_manifest() -> None:
    l1 = answer()["l1"] | {"ghost": {"state": "clear"}}
    assert failures(Stub(latest=latest_with(l1=l1))) == ["l1/extra-sensor"]


def test_refuses_l1_that_is_not_an_object() -> None:
    assert failures(Stub(latest=latest_with(l1=[]))) == ["l1/shape"]


def test_accepts_the_json_only_answer() -> None:
    assert failures(Stub(latest=no_frame_answer(), frame=None)) == []


def test_refuses_no_frame_part_while_a_sensor_is_not_unknown() -> None:
    latest = no_frame_answer()
    latest["l1"]["C3.A"] = {"state": "clear"}
    assert failures(Stub(latest=latest, frame=None)) == ["latest/no-frame-state"]


@pytest.mark.parametrize("field", ["frame_id", "captured_at"])
def test_refuses_a_frame_time_or_id_on_the_json_only_answer(field: str) -> None:
    latest = no_frame_answer() | {field: answer()[field]}
    rule = "latest/frame-id" if field == "frame_id" else "latest/captured-at"
    assert failures(Stub(latest=latest, frame=None)) == [rule]


def test_accepts_every_sensor_unknown_with_a_frame() -> None:
    latest = latest_with(l1=no_frame_answer()["l1"])
    assert failures(Stub(latest=latest)) == []


def test_refuses_a_frame_part_with_no_filename() -> None:
    assert failures(Stub(frame_filename=None)) == ["frame/no-filename"]


def test_refuses_a_frame_part_with_an_empty_filename() -> None:
    assert failures(Stub(frame_filename="")) == ["frame/no-filename"]


def test_refuses_a_wrong_frame_size() -> None:
    assert failures(Stub(frame=jpeg(WIDTH + 1, HEIGHT))) == ["frame/size"]


def test_refuses_a_frame_that_does_not_decode() -> None:
    assert failures(Stub(frame=b"not a jpeg")) == ["frame/jpeg"]


def test_refuses_a_frame_that_is_not_a_jpeg() -> None:
    out = io.BytesIO()
    Image.new("RGB", (WIDTH, HEIGHT)).save(out, "PNG")
    assert failures(Stub(frame=out.getvalue())) == ["frame/jpeg"]


def test_refuses_a_truncated_jpeg() -> None:
    assert failures(Stub(frame=jpeg()[:200])) == ["frame/jpeg"]


@pytest.mark.parametrize("version", [2, "1", 1.5, True, None])
def test_refuses_a_version_other_than_the_integer_1(version: Any) -> None:
    assert failures(Stub(latest=latest_with(version=version))) == ["latest/version"]


@pytest.mark.parametrize("frame_id", ["", 41, None])
def test_refuses_a_frame_id_with_a_frame(frame_id: Any) -> None:
    assert failures(Stub(latest=latest_with(frame_id=frame_id))) == ["latest/frame-id"]


@pytest.mark.parametrize(
    "captured_at",
    ["", 1762337730, None, "2026-11-05T10:15:30", "yesterday", "2026-13-45T10:15:30Z"],
)
def test_refuses_a_capture_time_with_a_frame(captured_at: Any) -> None:
    assert failures(Stub(latest=latest_with(captured_at=captured_at))) == ["latest/captured-at"]


def test_accepts_a_reader_ignored_key() -> None:
    assert failures(Stub(latest=latest_with(future={"anything": 1}))) == []


def test_refuses_a_missing_json_part() -> None:
    assert failures(Stub(json_name="other")) == ["latest/json-part"]


def test_refuses_a_json_part_with_a_filename() -> None:
    assert failures(Stub(json_filename="latest.json")) == ["latest/json-part"]


def test_refuses_a_json_part_that_is_not_an_object() -> None:
    assert failures(Stub(latest=[])) == ["latest/json-part"]


def test_refuses_nan_in_the_json_part() -> None:
    assert failures(Stub(raw_json=b'{"version": NaN}')) == ["latest/json-part"]


def test_refuses_an_answer_that_is_not_multipart() -> None:
    assert failures(Stub(latest_type="application/json")) == ["latest/content-type"]


def test_refuses_multipart_with_no_closing_delimiter() -> None:
    assert failures(Stub(truncate_latest=True)) == ["latest/multipart"]


def patched(latest_extra: dict[str, Any] | None = None) -> Stub:
    """A conforming service offering patches, with ``latest_extra`` in its JSON part."""
    parts = [
        Part("patch-0", jpeg(), "patch-0.jpg"),
        Part("patch-1", jpeg(), "patch-1.jpg"),
    ]
    latest = latest_with(patches={"C3.A": "patch-0", 'say "hi"': "patch-1"})
    return Stub(latest=latest | (latest_extra or {}), patch_parts=parts)


def test_accepts_patches() -> None:
    assert failures(patched()) == []


def test_refuses_a_patches_entry_naming_a_missing_part() -> None:
    stub = patched()
    stub.patch_parts.pop()
    assert failures(stub) == ["patches/missing-part"]


def test_refuses_a_patch_part_with_no_filename() -> None:
    stub = patched()
    stub.patch_parts[1] = Part("patch-1", jpeg())
    assert failures(stub) == ["patches/no-filename"]


def test_asks_for_patches_alongside_after() -> None:
    stub = patched()
    with serving(stub) as base:
        assert check(base, timeout=2) == []
    assert "/base/latest?after=f-41&patches=1" in stub.requests


def every_optional_field() -> dict[str, Any]:
    return {
        "l0": {
            "frame_id": "f-40",
            "captured_at": "2026-11-05T10:15:29.900+01:00",
            "detections": [],
        },
        "scores": {"C3.A": 0.93, 'say "hi"': 0.02},
        "threshold": 0.5,
        "changes": {"C3.A": 4, 'say "hi"': 0},
        "timings": {
            "capture_ms": 2.5,
            "decode_ms": 11,
            "cut_ms": 1.25,
            "classify_ms": 30,
            "rate_fps": 14.5,
        },
        "camera": {"state": "ok", "rate_fps": 15, "frame_age_ms": 20},
        "last_messages": {
            "C3.A": {
                "occupancy": {
                    "message": {"state": "occupied", "confidence": 0.93},
                    "time": "2026-11-05T10:15:30.125Z",
                },
                "text-profile": {"message": "ON", "time": "2026-11-05T10:15:30.125+00:00"},
            },
        },
    }


def test_accepts_every_optional_field() -> None:
    assert failures(patched(latest_extra=every_optional_field())) == []


def test_accepts_the_json_only_answer_with_optional_fields() -> None:
    latest = no_frame_answer() | every_optional_field()
    latest["camera"] = {"state": "lost"}
    assert failures(Stub(latest=latest, frame=None)) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("l0", []),
        ("l0", {"captured_at": "2026-11-05T10:15:29Z"}),
        ("l0", {"frame_id": "f-40"}),
        ("l0", {"frame_id": "f-40", "captured_at": "soon"}),
        ("scores", []),
        ("scores", {"C3.A": 1.5}),
        ("scores", {"C3.A": "high"}),
        ("scores", {"ghost": 0.5}),
        ("threshold", "0.5"),
        ("threshold", True),
        ("threshold", 2),
        ("patches", []),
        ("patches", {"C3.A": 0}),
        ("patches", {"C3.A": "frame"}),
        ("patches", {"ghost": "patch-0"}),
        ("changes", []),
        ("changes", {"C3.A": -1}),
        ("changes", {"C3.A": 1.5}),
        ("changes", {"C3.A": True}),
        ("changes", {"ghost": 1}),
        ("timings", []),
        ("timings", {"capture_ms": "2"}),
        ("timings", {"rate_fps": -1}),
        ("camera", []),
        ("camera", {"state": "fine"}),
        ("camera", {"rate_fps": "15"}),
        ("camera", {"frame_age_ms": -1}),
        ("last_messages", []),
        ("last_messages", {"ghost": {}}),
        ("last_messages", {"C3.A": []}),
        ("last_messages", {"C3.A": {"p": "ON"}}),
        ("last_messages", {"C3.A": {"p": {"time": "2026-11-05T10:15:30Z"}}}),
        ("last_messages", {"C3.A": {"p": {"message": "ON"}}}),
        ("last_messages", {"C3.A": {"p": {"message": "ON", "time": "soon"}}}),
        ("last_messages", {"C3.A": {"p": {"message": 1, "time": "2026-11-05T10:15:30Z"}}}),
    ],
)
def test_refuses_a_malformed_optional_field(field: str, value: Any) -> None:
    assert failures(patched(latest_extra={field: value})) == [f"optional/{field}"]


def test_accepts_a_key_it_does_not_know_inside_an_optional_field() -> None:
    assert failures(Stub(latest=latest_with(timings={"new_ms": "soon"}))) == []


@pytest.mark.parametrize(
    "stream",
    [
        b'data: {"sensor": "C3.A", "state": "clear"}\r\n\r\n'
        b'data: {"sensor": "say \\"hi\\"", "state": "clear"}\r\n\r\n',
        b': keep-alive\n\ndata:{"sensor": "C3.A", "state": "clear"}\n\n'
        b'data: {"sensor": "say \\"hi\\"",\ndata: "state": "clear"}\n\n',
    ],
)
def test_accepts_the_forms_of_a_server_sent_event(stream: bytes) -> None:
    assert failures(Stub(raw_events=stream)) == []


def test_refuses_events_missing_a_sensor_when_the_stream_closes() -> None:
    assert failures(Stub(events=events()[:1])) == ["events/missing-sensor"]


def test_refuses_events_missing_a_sensor_when_the_stream_stays_open() -> None:
    stub = Stub(events=events()[:1], hold_events=5)
    with serving(stub) as base:
        assert rules_at(base, timeout=0.2) == ["events/missing-sensor"]


def test_refuses_a_stream_of_only_keep_alives_by_the_deadline() -> None:
    stub = Stub(raw_events=b": keep-alive\n\n", repeat_events=True)
    with serving(stub) as base:
        assert rules_at(base, timeout=0.2) == ["events/missing-sensor"] * len(SENSORS)


def test_refuses_named_events_which_onmessage_does_not_see() -> None:
    stream = b"".join(f"event: l1\ndata: {json.dumps(event)}\n\n".encode() for event in events())
    assert failures(Stub(raw_events=stream)) == ["events/missing-sensor"] * len(SENSORS)


def test_the_worked_examples_in_live_md_conform() -> None:
    latest, stream = re.findall(r"```http\n(.*?)```", LIVE_MD, re.DOTALL)
    head, _, body = latest.partition("\n\n")
    no_frame = re.findall(r"```json\n(.*?)```", LIVE_MD, re.DOTALL)[0]
    archive = json.dumps(manifest(["C3.A", "C3.B"], 1920, 1080)).encode()
    raw_events = stream.partition("\n\n")[2].encode()
    raw_latest = body.replace("\n", "\r\n").encode().replace(b"<JPEG bytes>", jpeg(1920, 1080))
    latest_type = head.splitlines()[1].removeprefix("Content-Type: ")
    with_frame = Stub(
        manifest=archive, raw_latest=raw_latest, latest_type=latest_type, raw_events=raw_events
    )
    without_frame = Stub(
        manifest=archive, latest=json.loads(no_frame), frame=None, raw_events=raw_events
    )
    assert failures(with_frame) == []
    assert failures(without_frame) == []


def test_refuses_an_event_for_a_sensor_not_in_the_manifest() -> None:
    assert failures(Stub(events=[{"sensor": "ghost", "state": "clear"}, *events()])) == [
        "events/extra-sensor"
    ]


def test_applies_the_l1_rules_to_an_event() -> None:
    stream = [{"sensor": "C3.A", "state": "clear", "confidence": 0.1}, events()[1]]
    assert failures(Stub(events=stream)) == ["l1/clear-confidence"]


@pytest.mark.parametrize("data", ["not json", "[]", '{"state": "clear"}', '{"sensor": 7}'])
def test_refuses_an_event_that_is_not_an_entry_naming_a_sensor(data: str) -> None:
    stream = f"data: {data}\n\n".encode() + b"".join(
        f"data: {json.dumps(event)}\n\n".encode() for event in events()
    )
    assert failures(Stub(raw_events=stream)) == ["events/data"]


def test_refuses_events_with_the_wrong_content_type() -> None:
    assert failures(Stub(events_type="application/json")) == ["events/content-type"]


def test_refuses_a_manifest_with_the_wrong_content_type() -> None:
    assert failures(Stub(manifest_type="text/plain")) == ["archive/content-type"]


def test_accepts_a_manifest_content_type_with_a_charset() -> None:
    assert failures(Stub(manifest_type="application/json; charset=utf-8")) == []


def test_refuses_a_manifest_the_published_reader_refuses() -> None:
    assert failures(Stub(manifest=b'{"version": 4}')) == ["archive/manifest"]


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        ("/base/archive/manifest.json", "archive/status"),
        ("/base/latest", "latest/status"),
        ("/base/events", "events/status"),
    ],
)
def test_refuses_a_required_url_that_is_not_there(path: str, rule: str) -> None:
    assert failures(Stub(not_found={path})) == [rule]


def test_names_an_unreachable_service() -> None:
    with serving(Stub()) as base:
        pass
    assert rules_at(base, timeout=0.2) == ["http/unreachable"]


def test_accepts_a_base_url_with_a_trailing_slash() -> None:
    with serving(Stub()) as base:
        assert check(f"{base}/", timeout=2) == []


def test_the_command_exits_zero_on_a_conforming_service(capsys: pytest.CaptureFixture[str]) -> None:
    with serving(Stub()) as base:
        main([base])
    assert "conforms" in capsys.readouterr().out


def test_the_command_exits_non_zero_and_names_each_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving(Stub(frame_filename=None, frame=jpeg(WIDTH + 1, HEIGHT))) as base:
        with pytest.raises(SystemExit) as exit_info:
            main([base])
    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "frame/no-filename" in out
    assert "frame/size" in out


def test_refuses_a_stray_part_on_the_json_only_answer() -> None:
    stub = Stub(
        latest=no_frame_answer(), frame=None, patch_parts=[Part("patch-0", jpeg(), "patch-0.jpg")]
    )
    assert failures(stub) == ["latest/no-frame-parts"]


@pytest.mark.parametrize(
    ("field", "rule"), [("frame_id", "latest/frame-id"), ("captured_at", "latest/captured-at")]
)
def test_refuses_an_absent_frame_time_or_id_on_the_json_only_answer(field: str, rule: str) -> None:
    latest = no_frame_answer()
    del latest[field]
    assert failures(Stub(latest=latest, frame=None)) == [rule]


def test_refuses_patches_dropped_when_asked_alongside_after() -> None:
    stub = patched()
    stub.patches_with_after = False
    assert failures(stub) == ["patches/after"]


def test_refuses_two_sensors_naming_one_part() -> None:
    assert failures(patched({"patches": {"C3.A": "patch-0", 'say "hi"': "patch-0"}})) == [
        "optional/patches"
    ]


def test_names_a_number_too_large_for_a_float() -> None:
    assert failures(Stub(latest=l1_with({"state": "occupied", "confidence": 10**400}))) == [
        "l1/occupied-confidence"
    ]
    assert failures(Stub(latest=latest_with(timings={"cut_ms": 10**400}))) == ["optional/timings"]


def test_names_an_infinite_number() -> None:
    raw = json.dumps(latest_with(threshold=0)).replace('"threshold": 0', '"threshold": 1e999')
    assert failures(Stub(raw_json=raw.encode())) == ["optional/threshold"]


def test_names_json_nested_too_deep_to_read() -> None:
    assert failures(Stub(raw_json=b"[" * 200_000)) == ["latest/json-part"]


def test_names_a_frame_too_large_to_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    assert failures(Stub()) == ["frame/jpeg"]


def save_rules(stub: Stub) -> list[str]:
    """The rules ``stub`` breaks with ``--save``. Each one must be a rule LIVE.md lists."""
    with serving(stub) as base:
        rules = [failure.rule for failure in check(base, timeout=2, save=True)]
    for rule in rules:
        assert f"`{rule}`" in LIVE_MD
    return rules


def test_without_the_flag_the_checker_writes_nothing() -> None:
    stub = Stub(offers_saving=True, checks_if_match=False)
    before = stub.manifest
    with serving(stub) as base:
        assert check(base, timeout=2) == []
    assert stub.writes == []
    assert stub.manifest == before


def test_save_accepts_a_service_that_saves_and_leaves_the_manifest_as_it_was() -> None:
    stub = Stub(offers_saving=True)
    before = stub.manifest
    assert save_rules(stub) == []
    assert stub.writes == [before]
    assert stub.manifest == before


def test_save_accepts_a_service_with_no_etag_that_answers_405() -> None:
    assert save_rules(Stub()) == []


@pytest.mark.parametrize(
    "stub",
    [Stub(not_offered_status=204), Stub(not_offered_status=501), Stub(not_offered_allow=None)],
)
def test_save_refuses_a_service_with_no_etag_that_does_not_answer_405(stub: Stub) -> None:
    assert save_rules(stub) == ["save/not-offered"] * 2  # One for PUT, and one for DELETE.


def test_save_refuses_a_stub_that_skips_if_match() -> None:
    assert save_rules(Stub(offers_saving=True, checks_if_match=False)) == [
        "save/if-match-missing",
        "save/if-match-stale",
    ]


def test_save_refuses_a_stub_that_skips_the_origin_check() -> None:
    assert save_rules(Stub(offers_saving=True, checks_origin=False)) == ["save/origin"]


def test_save_refuses_a_stub_that_takes_a_bad_manifest_and_puts_the_old_one_back() -> None:
    stub = Stub(offers_saving=True, checks_manifest=False)
    before = stub.manifest
    assert save_rules(stub) == ["save/manifest-refused"]
    assert stub.manifest == before


def test_save_refuses_a_422_with_no_error_field() -> None:
    assert save_rules(Stub(offers_saving=True, no_error_field=True)) == ["save/manifest-refused"]


def test_save_refuses_an_etag_that_is_not_the_sha_256_of_the_bytes() -> None:
    rules = save_rules(Stub(offers_saving=True, wrong_etag='"not-the-digest"'))
    assert rules == ["save/etag"]


def test_save_refuses_a_rewrite_that_fails() -> None:
    assert save_rules(Stub(offers_saving=True, rewrite_fails=True)) == ["save/rewrite"]


def test_the_command_takes_the_save_flag(capsys: pytest.CaptureFixture[str]) -> None:
    stub = Stub(offers_saving=True, checks_origin=False)
    with serving(stub) as base:
        with pytest.raises(SystemExit):
            main([base, "--save"])
    assert "save/origin" in capsys.readouterr().out
