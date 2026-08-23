import esphome.codegen as cg
from esphome.components import sensor
import esphome.config_validation as cv
from esphome.const import (
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_DURATION,
    DEVICE_CLASS_FREQUENCY,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL_INCREASING,
    UNIT_AMPERE,
    UNIT_HERTZ,
    UNIT_SECOND,
)

from . import CONF_EASYSTART_ID, EasyStart

DEPENDENCIES = ["easystart"]

CONF_LIVE_CURRENT = "live_current"
CONF_LAST_START_PEAK = "last_start_peak"
CONF_LINE_FREQUENCY = "line_frequency"
CONF_LEARNED_STARTS = "learned_starts"
CONF_TOTAL_STARTS = "total_starts"
CONF_TOTAL_FAULTS = "total_faults"
CONF_SCPT_REMAINING = "scpt_remaining"
CONF_STATE_CODE = "state_code"


def _current(name):
    return sensor.sensor_schema(
        unit_of_measurement=UNIT_AMPERE,
        accuracy_decimals=1,
        device_class=DEVICE_CLASS_CURRENT,
        state_class=STATE_CLASS_MEASUREMENT,
    )


def _count(total=False):
    return sensor.sensor_schema(
        accuracy_decimals=0,
        state_class=STATE_CLASS_TOTAL_INCREASING if total else STATE_CLASS_MEASUREMENT,
        entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
    )


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_EASYSTART_ID): cv.use_id(EasyStart),
        cv.Optional(CONF_LIVE_CURRENT): _current(CONF_LIVE_CURRENT),
        cv.Optional(CONF_LAST_START_PEAK): _current(CONF_LAST_START_PEAK),
        cv.Optional(CONF_LINE_FREQUENCY): sensor.sensor_schema(
            unit_of_measurement=UNIT_HERTZ,
            accuracy_decimals=1,
            device_class=DEVICE_CLASS_FREQUENCY,
            state_class=STATE_CLASS_MEASUREMENT,
        ),
        cv.Optional(CONF_SCPT_REMAINING): sensor.sensor_schema(
            unit_of_measurement=UNIT_SECOND,
            accuracy_decimals=0,
            device_class=DEVICE_CLASS_DURATION,
            state_class=STATE_CLASS_MEASUREMENT,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
        cv.Optional(CONF_LEARNED_STARTS): _count(),
        cv.Optional(CONF_TOTAL_STARTS): _count(total=True),
        cv.Optional(CONF_TOTAL_FAULTS): _count(total=True),
        cv.Optional(CONF_STATE_CODE): _count(),
    }
)

_SETTERS = {
    CONF_LIVE_CURRENT: "set_live_current",
    CONF_LAST_START_PEAK: "set_last_start_peak",
    CONF_LINE_FREQUENCY: "set_line_frequency",
    CONF_LEARNED_STARTS: "set_learned_starts",
    CONF_TOTAL_STARTS: "set_total_starts",
    CONF_TOTAL_FAULTS: "set_total_faults",
    CONF_SCPT_REMAINING: "set_scpt_remaining",
    CONF_STATE_CODE: "set_state_code",
}


async def to_code(config):
    hub = await cg.get_variable(config[CONF_EASYSTART_ID])
    for key, setter in _SETTERS.items():
        if key in config:
            sens = await sensor.new_sensor(config[key])
            cg.add(getattr(hub, setter)(sens))
