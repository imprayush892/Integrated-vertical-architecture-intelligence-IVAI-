import json
import os
from importlib import resources

import pytest


@pytest.fixture(autouse=True)
def planner_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANNER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLANNER_GEOCODER", "offline")
    monkeypatch.setenv("CLAUDE_DESKTOP_CONFIG", str(tmp_path / "claude" / "claude_desktop_config.json"))
    yield tmp_path


@pytest.fixture
def lokhandwala():
    return json.loads(resources.files("planner_engine").joinpath("data/golden/lokhandwala.json").read_text())


@pytest.fixture
def footprint():
    from planner_engine.models import PlateFootprint

    def make(w=52.9, d=29.0):
        return PlateFootprint(id="T1", polygon=[], angle_deg=0, center=(0, 0), width=w, depth=d, area=w * d)

    return make
