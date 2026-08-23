import esphome.codegen as cg
from esphome.components import text_sensor
import esphome.config_validation as cv
from esphome.const import ENTITY_CATEGORY_DIAGNOSTIC

from . import CONF_EASYSTART_ID, EasyStart

DEPENDENCIES = ["easystart"]

CONF_SYSTEM_STATE = "system_state"
CONF_MODEL = "model"
CONF_FIRMWARE = "firmware"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_EASYSTART_ID): cv.use_id(EasyStart),
        cv.Optional(CONF_SYSTEM_STATE): text_sensor.text_sensor_schema(),
        cv.Optional(CONF_MODEL): text_sensor.text_sensor_schema(
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC
        ),
        cv.Optional(CONF_FIRMWARE): text_sensor.text_sensor_schema(
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC
        ),
    }
)

_SETTERS = {
    CONF_SYSTEM_STATE: "set_system_state",
    CONF_MODEL: "set_model",
    CONF_FIRMWARE: "set_firmware",
}


async def to_code(config):
    hub = await cg.get_variable(config[CONF_EASYSTART_ID])
    for key, setter in _SETTERS.items():
        if key in config:
            ts = await text_sensor.new_text_sensor(config[key])
            cg.add(getattr(hub, setter)(ts))
