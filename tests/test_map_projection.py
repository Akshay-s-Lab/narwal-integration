"""The map camera's calibration points match the pixels it draws."""

from __future__ import annotations

import io

from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from PIL import Image

from custom_components.narwal.narwal_client.map_renderer import (
    COLOR_ROBOT,
    COLOR_ROBOT_OUTLINE,
    COLOR_STATION,
    project_to_image,
)

from .test_integration import setup  # noqa: F401


def _solve_affine(points: list[dict]) -> tuple[float, ...]:
    (x0, y0), (x1, y1), (x2, y2) = ((p["vacuum"]["x"], p["vacuum"]["y"]) for p in points)
    (u0, v0), (u1, v1), (u2, v2) = ((p["map"]["x"], p["map"]["y"]) for p in points)
    det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    a = ((u1 - u0) * (y2 - y0) - (u2 - u0) * (y1 - y0)) / det
    b = ((u2 - u0) * (x1 - x0) - (u1 - u0) * (x2 - x0)) / det
    c = ((v1 - v0) * (y2 - y0) - (v2 - v0) * (y1 - y0)) / det
    d = ((v2 - v0) * (x1 - x0) - (v1 - v0) * (x2 - x0)) / det
    return a, b, u0 - a * x0 - b * y0, c, d, v0 - c * x0 - d * y0


async def test_calibration_points_land_on_drawn_markers(hass: HomeAssistant, setup) -> None:  # noqa: F811
    camera = er.async_get(hass).async_get_entity_id("camera", "narwal", "abcdef1234567890abcdef1234567890_map")
    snapshot = setup.client.state.map
    assert snapshot.rotate_angle == 90

    image = Image.open(io.BytesIO((await async_get_image(hass, camera)).content)).convert("RGB")
    a, b, tx, c, d, ty = _solve_affine(hass.states.get(camera).attributes["calibration_points"])

    station = snapshot.station
    px, py = a * station.x + b * station.y + tx, c * station.x + d * station.y + ty
    assert (round(px, 3), round(py, 3)) == tuple(round(v, 3) for v in project_to_image(snapshot, station.x, station.y))
    assert image.getpixel((int(px), int(py))) in (COLOR_STATION, COLOR_ROBOT, COLOR_ROBOT_OUTLINE)
    assert image.getpixel((0, 0)) not in (COLOR_STATION, COLOR_ROBOT, COLOR_ROBOT_OUTLINE)
