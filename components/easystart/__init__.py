"""Micro-Air EasyStart soft starter over BLE.

Protocol reverse-engineered from net.microair.easystart 4.3 - see
easystart-ble-protocol.md. The device has no authentication of any kind.
"""

import esphome.codegen as cg
from esphome.components import ble_client
import esphome.config_validation as cv
from esphome.const import CONF_ID

CODEOWNERS = ["@rar"]
DEPENDENCIES = ["ble_client"]
MULTI_CONF = True

CONF_EASYSTART_ID = "easystart_id"

easystart_ns = cg.esphome_ns.namespace("easystart")
EasyStart = easystart_ns.class_(
    "EasyStart", cg.PollingComponent, ble_client.BLEClientNode
)

CONFIG_SCHEMA = (
    cv.Schema({cv.GenerateID(): cv.declare_id(EasyStart)})
    .extend(cv.polling_component_schema("10s"))
    .extend(ble_client.BLE_CLIENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await ble_client.register_ble_node(var, config)
