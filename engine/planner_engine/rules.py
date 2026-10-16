"""Rules files: statutory tables and clause records loaded from YAML (never hard-coded).

* ``data/rules/site.yaml``    - setback / tower gap / coverage tables
* ``data/rules/design.yaml``  - architect-authored design defaults (not statutory)
* ``data/rules/<code>/*.yaml`` - one record per clause: id, source, test, threshold, applies-to

An office can override any file by placing a copy in ``$PLANNER_RULES_DIR``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOM_TYPES = {
    "living", "dining", "kitchen", "master", "bedroom", "study", "pooja", "toilet", "bath", "wc",
    "foyer", "passage", "utility", "balcony", "dress", "store",
}
HABITABLE = {"living", "master", "bedroom", "study"}
WET = {"toilet", "bath", "wc", "kitchen", "utility"}
METRICS = {
    "area", "width", "area_and_width", "max_area", "window_ratio", "ventilated", "passage_width",
    "travel_distance", "stair_count", "stair_width", "entry_door_width", "advisory",
}


class Clause(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    clause: str
    title: str
    scope: Literal["room", "unit", "floor", "building"]
    metric: str
    types: list[str] = Field(default_factory=list)
    op: Literal[">=", "<=", "==", ">", "<"]
    threshold: Any
    unit: str = ""
    review_band: float = 0.0
    when: str | None = None
    advice: str | None = None
    code: str = ""
    source: str = ""
    version: str = ""
    verified: bool = False

    @field_validator("metric")
    @classmethod
    def _metric(cls, v: str) -> str:
        if v not in METRICS:
            raise ValueError(f"unknown metric {v!r}")
        return v

    @field_validator("types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in ROOM_TYPES]
        if bad:
            raise ValueError(f"unknown room types {bad}")
        return v

    def applies(self, ctx: dict[str, float]) -> bool:
        if not self.when:
            return True
        lhs, op, rhs = self.when.split()
        a, b = float(ctx.get(lhs, 0.0)), float(rhs)
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "==": a == b}[op]


def _data_dir() -> Path:
    return Path(str(resources.files("planner_engine").joinpath("data/rules")))


def _override_dir() -> Path | None:
    d = os.environ.get("PLANNER_RULES_DIR")
    return Path(d) if d else None


def _read(rel: str) -> Any:
    o = _override_dir()
    if o and (o / rel).exists():
        return yaml.safe_load((o / rel).read_text())
    return yaml.safe_load((_data_dir() / rel).read_text())


@lru_cache(maxsize=1)
def site_rules() -> dict[str, Any]:
    return _read("site.yaml")


@lru_cache(maxsize=1)
def design() -> dict[str, Any]:
    return _read("design.yaml")


@lru_cache(maxsize=1)
def priors() -> dict[str, Any]:
    return _read("layout_priors.yaml")


@lru_cache(maxsize=1)
def clauses() -> list[Clause]:
    out: list[Clause] = []
    seen: set[str] = set()
    roots = [_data_dir()]
    o = _override_dir()
    if o:
        roots.insert(0, o)
    files: dict[str, Path] = {}
    for root in reversed(roots):  # override dir wins
        for p in sorted(root.glob("*/*.yaml")):
            files[str(p.relative_to(root))] = p
    for _, p in sorted(files.items()):
        doc = yaml.safe_load(p.read_text())
        for c in doc.get("clauses", []):
            clause = Clause(
                **c, code=doc["code"], source=doc.get("source", ""), version=str(doc.get("version", "")),
                verified=bool(doc.get("verified", False)),
            )
            if clause.id in seen:
                raise ValueError(f"duplicate clause id {clause.id} in {p}")
            seen.add(clause.id)
            out.append(clause)
    return out


def _lookup(table: list[list[float]], key: float) -> float:
    for limit, value in table:
        if key <= limit:
            return float(value)
    return float(table[-1][1])


def setback_for(height: float, road_width: float) -> tuple[float, float, str]:
    """Return (side/rear setback, front setback, clause)."""
    s = site_rules()["setback"]
    side = _lookup(s["by_height"], height)
    front = max(side, _lookup(s["front_by_road_width"], road_width))
    return side, front, s["clause"]


def tower_gap_for(height: float) -> tuple[float, str]:
    g = site_rules()["tower_gap"]
    return _lookup(g["by_height"], height), g["clause"]


def max_coverage() -> tuple[float, str]:
    c = site_rules()["coverage"]
    return float(c["max"]), c["clause"]


def reload() -> None:
    site_rules.cache_clear()
    design.cache_clear()
    priors.cache_clear()
    clauses.cache_clear()
