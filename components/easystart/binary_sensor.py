import esphome.codegen as cg
from esphome.components import binary_sensor
import esphome.config_validation as cv
from esphome.const import DEVICE_CLASS_PROBLEM

from . import CONF_EASYSTART_ID, EasyStart

DEPENDENCIES = ["easystart"]

CONF_FAULT = "fault"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_EASYSTART_ID): cv.use_id(EasyStart),
        cv.Optional(CONF_FAULT): binary_sensor.binary_sensor_schema(
            device_class=DEVICE_CLASS_PROBLEM
        ),
    }
)


async def to_code(config):
    hub = await cg.get_variable(config[CONF_EASYSTART_ID])
    if CONF_FAULT in config:
        bs = await binary_sensor.new_binary_sensor(config[CONF_FAULT])
        cg.add(hub.set_fault(bs))
