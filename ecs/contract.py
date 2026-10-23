"""Data contract for the screening API (F1, F2, D1).

Validation happens here, at the ingestion boundary, before any run is created
or queued. A request that fails any rule is quarantined with field-level
errors and no agent run starts.

The `schedule_item` mapping must be confirmed against the downloaded EIA
Notification, 2006 (Stage 1 task). Sectors not listed here, or `other`, are
outside the corpus and are escalated (F7).
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "1.0.0"


class Sector(str, Enum):
    mining_minerals = "mining_minerals"
    thermal_power = "thermal_power"
    cement = "cement"
    petroleum_refining = "petroleum_refining"
    building_construction = "building_construction"
    township = "township"
    other = "other"


class CapacityUnit(str, Enum):
    MTPA = "MTPA"   # million tonnes per annum
    MW = "MW"       # megawatts
    SQM = "SQM"     # square metres of built-up area
    TPA = "TPA"     # tonnes per annum


# Schedule item for each sector - CONFIRM AGAINST THE PDF (Stage 1).
SECTOR_SCHEDULE_ITEM: dict[Sector, Optional[str]] = {
    Sector.mining_minerals: "1(a)",
    Sector.thermal_power: "1(d)",
    Sector.cement: "3(b)",
    Sector.petroleum_refining: "4(a)",
    Sector.building_construction: "8(a)",
    Sector.township: "8(b)",
    Sector.other: None,
}

# Which measure each sector's thresholds use.
SECTOR_UNITS: dict[Sector, set[CapacityUnit]] = {
    Sector.thermal_power: {CapacityUnit.MW},
    Sector.cement: {CapacityUnit.MTPA},
    Sector.petroleum_refining: {CapacityUnit.MTPA, CapacityUnit.TPA},
    Sector.building_construction: {CapacityUnit.SQM},
    Sector.township: {CapacityUnit.SQM},
    Sector.mining_minerals: {CapacityUnit.MTPA, CapacityUnit.TPA},
}
CAPACITY_REQUIRED = {Sector.thermal_power, Sector.cement, Sector.building_construction}
AREA_REQUIRED = {Sector.mining_minerals, Sector.township}

STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab",
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh",
    "Lakshadweep", "Puducherry",
]
State = Enum("State", {s.replace(" ", "_").lower(): s for s in STATES}, type=str)


class Capacity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float = Field(..., gt=0, le=1_000_000, description="Production or built-up capacity")
    unit: CapacityUnit


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: State
    district: str = Field(..., min_length=2, max_length=100)


class ScreeningRequest(BaseModel):
    """A project description submitted for screening."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_name: str = Field(..., min_length=3, max_length=200)
    sector: Sector
    description: str = Field(..., min_length=1, max_length=2000,
                             description="Free text. Checked for prompt injection and toxicity.")
    capacity: Optional[Capacity] = None
    area_ha: Optional[float] = Field(None, gt=0, le=100_000, description="Project or lease area, hectares")
    location: Location
    protected_area_distance_km: Optional[float] = Field(
        ..., ge=0, le=500,
        description="Distance to the nearest protected area boundary. null = unknown (escalates).")
    protected_area_name: Optional[str] = Field(None, min_length=2, max_length=200)
    interstate_boundary_distance_km: Optional[float] = Field(None, ge=0, le=2000)
    callback_url: Optional[str] = Field(None, max_length=500, description="Reserved; clients poll.")

    @field_validator("project_name", "description")
    @classmethod
    def no_control_chars(cls, v: str) -> str:
        if any(ord(c) < 32 and c not in "\n\t\r" for c in v):
            raise ValueError("contains control characters")
        return v

    @model_validator(mode="after")
    def cross_field(self) -> "ScreeningRequest":
        if self.capacity is not None and self.sector in SECTOR_UNITS:
            allowed = SECTOR_UNITS[self.sector]
            if self.capacity.unit not in allowed:
                raise ValueError(
                    f"capacity.unit {self.capacity.unit.value} is not valid for sector "
                    f"{self.sector.value}; allowed: {sorted(u.value for u in allowed)}")
        if self.sector in CAPACITY_REQUIRED and self.capacity is None:
            raise ValueError(f"capacity is required for sector {self.sector.value}")
        if self.sector in AREA_REQUIRED and self.area_ha is None:
            raise ValueError(f"area_ha is required for sector {self.sector.value}")
        if self.protected_area_distance_km is not None and not self.protected_area_name:
            raise ValueError("protected_area_name is required when protected_area_distance_km is given")
        return self

    @property
    def schedule_item(self) -> Optional[str]:
        return SECTOR_SCHEDULE_ITEM.get(self.sector)

    def normalized(self) -> dict:
        """Canonical dict used for caching and for the agent's request view."""
        d = self.model_dump(mode="json", exclude={"callback_url"})
        d["schedule_item"] = self.schedule_item
        return d


Route = Literal["no_clearance_required", "category_b_state", "category_a_central", "escalate_to_human"]


class Citation(BaseModel):
    passage_id: str
    clause_id: Optional[str] = None
    document: Optional[str] = None
    page: Optional[int] = None
    text: str


class ScreeningResult(BaseModel):
    run_id: str
    status: Literal["queued", "running", "done", "failed"]
    route: Optional[Route] = None
    category: Optional[Literal["A", "B", "none"]] = None
    basis: Optional[str] = None
    citations: list[Citation] = []
    conditions_applied: list[str] = []
    escalation_reason: Optional[str] = None
    verified: Optional[bool] = None
    prompt_version: Optional[str] = None
    cache_hit: bool = False
    disclaimer: str = ("Screening opinion only, not an authoritative determination. "
                       "An accredited consultant decides.")


def field_errors(exc) -> list[dict]:
    """Turn a pydantic ValidationError into field-level errors for quarantine."""
    out = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", ())) or "(request)"
        out.append({"field": loc, "error": e.get("msg"), "type": e.get("type")})
    return out
