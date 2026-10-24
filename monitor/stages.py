"""Scenario stage handlers and audience metadata."""

from collections.abc import Awaitable, Callable
from typing import Any


StageHandler = Callable[[Any, dict[str, Any]], Awaitable[None]]

STAGE_AUDIENCES: dict[str, dict[str, Any]] = {
    "pre_block": {},
    "d_minus_1_ready": {},
    "d_minus_1_docs": {},
    "d1_10_congrats": {},
    "d1_10_email": {},
    "d1_11_map": {"sites": {"GTP", "Luzhniki"}},
    "d1_12_access": {},
    "d1_1201_postcard": {},
    "d1_1230_lunch": {},
    "d1_13_directory": {},
    "d1_15_presentation": {},
    "d1_18_end": {"office": True},
    "d2_10_selfintro": {},
    "d3_09_plan": {"office": True},
    "d3_11_gym": {"sites": {"GTP"}},
    "d3_1730_survey": {},
    "d10_events": {"sites": {"GrPP", "Luzhniki"}},
    "w2_check": {},
    "m2_start": {},
    "m15_reminder_films": {},
    "m17_survey": {},
    "m3_start": {},
    "m25_books": {"office": True},
    "final_survey": {},
}


def _stage_handler(stage_key: str) -> StageHandler:
    async def handle(flows: Any, employee: dict[str, Any]) -> None:
        await flows._run_scenario_stage(stage_key, employee)

    return handle


async def _pre_block_handler(flows: Any, employee: dict[str, Any]) -> None:
    await flows._begin_pre_block(str(employee["max_user_id"]))


STAGE_HANDLERS: dict[str, StageHandler] = {
    stage_key: _stage_handler(stage_key) for stage_key in STAGE_AUDIENCES
}
STAGE_HANDLERS["pre_block"] = _pre_block_handler


def is_audience_eligible(stage_key: str, employee: dict[str, Any]) -> bool:
    audience = STAGE_AUDIENCES[stage_key]
    if audience.get("sites") and employee.get("site") not in audience["sites"]:
        return False
    if audience.get("office") and not employee.get("is_office"):
        return False
    return True
