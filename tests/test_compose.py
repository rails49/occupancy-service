"""The compose project a box runs, as `docker compose config` resolves it. This needs the docker
CLI and no daemon."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent

BOX_ENV = {"BOX_DOMAIN": "box.example"}

OCCUPANCY_ENV = {
    "OCCUPANCY_ARCHIVE": "/srv/layout.r49",
    "OCCUPANCY_CAMERA": "/dev/v4l/by-id/usb-camera-video-index0",
    "TC49_UID": "1000",
    "TC49_GID": "1000",
    "VIDEO_GID": "44",
    "RENDER_GID": "992",
}

SETTINGS = BOX_ENV | OCCUPANCY_ENV


def config(
    settings: dict[str, str], env_files: list[Path] | None = None
) -> subprocess.CompletedProcess[str]:
    flags = [arg for path in env_files or [] for arg in ("--env-file", str(path))]
    return subprocess.run(
        ["docker", "compose", *flags, "-f", "compose.yaml", "config", "--format", "json"],
        cwd=ROOT,
        env={**{k: v for k, v in os.environ.items() if k not in SETTINGS}, **settings},
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def project() -> dict[str, Any]:
    done = config(SETTINGS)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def labels(project: dict[str, Any], name: str) -> dict[str, str]:
    return project["services"][name]["labels"]


def test_no_container_publishes_a_port(project: dict[str, Any]) -> None:
    assert [s.get("ports") for s in project["services"].values()] == [None, None]


def test_both_containers_join_the_external_rails49_network(project: dict[str, Any]) -> None:
    assert project["networks"]["rails49"]["external"] is True
    for service in project["services"].values():
        assert list(service["networks"]) == ["rails49"]


def test_the_box_domain_goes_to_the_page(project: dict[str, Any]) -> None:
    page = labels(project, "page")
    assert page["traefik.http.routers.occupancy.rule"] == "Host(`occupancy.box.example`)"
    assert page["traefik.http.services.occupancy.loadbalancer.server.port"] == "80"


def test_live_goes_to_the_service_unstripped(project: dict[str, Any]) -> None:
    service = labels(project, "service")
    rule = service["traefik.http.routers.occupancy-live.rule"]
    assert rule == "Host(`occupancy.box.example`) && PathPrefix(`/live`)"
    assert service["traefik.http.services.occupancy-live.loadbalancer.server.port"] == "8000"
    assert not any("middlewares" in key for key in service)


@pytest.mark.parametrize(
    ("container", "router", "rule", "service", "port"),
    [
        ("page", "occupancy-http", "PathPrefix(`/`)", "occupancy", "80"),
        ("service", "occupancy-live-http", "PathPrefix(`/live`)", "occupancy-live", "8000"),
    ],
)
def test_the_ui_port_serves_each_router_over_plain_http(
    project: dict[str, Any], container: str, router: str, rule: str, service: str, port: str
) -> None:
    found = labels(project, container)
    prefix = f"traefik.http.routers.{router}."
    assert found[f"traefik.http.services.{service}.loadbalancer.server.port"] == port
    assert {k: v for k, v in found.items() if k.startswith(prefix)} == {
        prefix + "rule": rule,
        prefix + "entrypoints": "occupancy",
        prefix + "service": service,
    }


def test_the_websecure_routers_keep_their_certificate(project: dict[str, Any]) -> None:
    for container, router in [("page", "occupancy"), ("service", "occupancy-live")]:
        found = labels(project, container)
        assert found[f"traefik.http.routers.{router}.entrypoints"] == "websecure"
        assert found[f"traefik.http.routers.{router}.tls.certresolver"] == "le"


def test_box_domain_may_be_empty() -> None:
    done = config({**SETTINGS, "BOX_DOMAIN": ""})
    assert done.returncode == 0, done.stderr


def test_a_replugged_camera_appears_with_no_restart(project: dict[str, Any]) -> None:
    service = project["services"]["service"]
    assert "c 81:* rmw" in service["device_cgroup_rules"]
    dev = [v for v in service["volumes"] if v["target"] == "/dev"]
    assert [(v["type"], v["source"]) for v in dev] == [("bind", "/dev")]


def test_the_service_reaches_the_gpu_and_the_camera_as_the_person(
    project: dict[str, Any],
) -> None:
    service = project["services"]["service"]
    assert "c 226:* rmw" in service["device_cgroup_rules"]
    assert service["group_add"] == ["44", "992"]


def test_the_service_restarts_unless_stopped(project: dict[str, Any]) -> None:
    assert project["services"]["service"]["restart"] == "unless-stopped"


def test_the_service_dials_the_broker_and_runs_two_profiles(project: dict[str, Any]) -> None:
    command = project["services"]["service"]["command"]
    assert "--broker=broker:1883" in command
    assert [c for c in command if c.startswith("--profile=")] == [
        "--profile=occupancy",
        "--profile=control",
    ]


def test_the_service_pauses_on_controls_track_power(project: dict[str, Any]) -> None:
    command = project["services"]["service"]["command"]
    assert "--power-topic=tc49/layout/state/power" in command


def test_the_service_picks_its_engine_and_device(project: dict[str, Any]) -> None:
    command = project["services"]["service"]["command"]
    assert [c for c in command if c.startswith(("--engine", "--device"))] == []


def test_the_archive_is_bound_read_write_as_the_person(project: dict[str, Any]) -> None:
    service = project["services"]["service"]
    archive = [v for v in service["volumes"] if v["source"] == "/srv/layout.r49"]
    assert len(archive) == 1
    assert not archive[0].get("read_only")
    assert service["user"] == "1000:1000"


@pytest.mark.parametrize("role", ["service", "page"])
def test_a_local_build_is_named_dev_with_no_revision(project: dict[str, Any], role: str) -> None:
    found = project["services"][role]
    assert found["image"] == f"ghcr.io/rails49/occupancy-{role}:dev"
    assert found["build"]["args"]["OCCUPANCY_SERVICE_COMMIT"] == ""


@pytest.mark.parametrize("role", ["service", "page"])
def test_a_build_at_a_commit_is_named_by_it_and_carries_it(role: str) -> None:
    commit = "0123456789abcdef0123456789abcdef01234567"
    done = config({**SETTINGS, "OCCUPANCY_SERVICE_COMMIT": commit})
    found = json.loads(done.stdout)["services"][role]
    assert found["image"] == f"ghcr.io/rails49/occupancy-{role}:{commit}"
    assert found["build"]["args"]["OCCUPANCY_SERVICE_COMMIT"] == commit


def test_compose_leaves_the_detector_version_to_the_dockerfile(project: dict[str, Any]) -> None:
    assert "DETECTOR_VERSION" not in project["services"]["service"]["build"]["args"]


@pytest.mark.parametrize(
    ("missing", "file"),
    [(k, "/etc/rails49/box.env") for k in BOX_ENV]
    + [(k, "/etc/rails49/occupancy.env") for k in OCCUPANCY_ENV],
)
def test_a_setting_with_no_default_is_named_with_its_file_when_missing(
    missing: str, file: str
) -> None:
    done = config({k: v for k, v in SETTINGS.items() if k != missing})
    assert done.returncode != 0
    assert missing in done.stderr
    assert file in done.stderr


def test_the_box_env_and_occupancy_env_hold_every_setting(tmp_path: Path) -> None:
    files = []
    for name, settings in [("box.env", BOX_ENV), ("occupancy.env", OCCUPANCY_ENV)]:
        path = tmp_path / name
        path.write_text("".join(f"{k}={v}\n" for k, v in settings.items()))
        files.append(path)
    done = config({}, files)
    assert done.returncode == 0, done.stderr
    service = json.loads(done.stdout)["services"]["service"]
    assert service["user"] == "1000:1000"
    assert "--camera=/dev/v4l/by-id/usb-camera-video-index0" in service["command"]
