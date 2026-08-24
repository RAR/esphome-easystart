# `easystart` — ESPHome external component

Reads a Micro-Air EasyStart soft starter over BLE, directly from an ESP32.
Protocol details: [`easystart-ble-protocol.md`](easystart-ble-protocol.md).

## Why this instead of a Bluetooth proxy

A proxy forwards a connection to Home Assistant, which then needs a custom
integration to speak the protocol. This component puts the protocol on the ESP32
itself, so the EasyStart shows up as ordinary ESPHome entities over the native
API — no custom integration, no HACS, no Python to maintain.

Trade-off: the ESP32 holds the BLE connection continuously, so it is dedicated
to this job. It can still run a `bluetooth_proxy` alongside, but the EasyStart
connection consumes one of the ESP32's three connection slots.

## Layout

```
components/easystart/
  __init__.py        hub component, attaches to a ble_client
  sensor.py          numeric entities
  text_sensor.py     state / model / firmware
  binary_sensor.py   fault flag
  easystart.h/.cpp   the BLE protocol itself
easystart.yaml       example configuration (one unit)
easystart-dual.yaml  example: two units on one ESP32, with RSSI diagnostics
tools/easystart_ble.py   standalone Python client (scan / read from a laptop)
easystart-ble-protocol.md   the reverse-engineered protocol
```

## Use

Point `external_components` at the `components` directory and set your device's
MAC. See `easystart.yaml` for a complete example.

```yaml
external_components:
  - source:
      type: local
      path: components

esp32_ble_tracker:

ble_client:
  - mac_address: AA:BB:CC:DD:EE:FF
    id: easystart_ble

easystart:
  id: easystart_hub
  ble_client_id: easystart_ble
  update_interval: 10s

sensor:
  - platform: easystart
    easystart_id: easystart_hub
    live_current:
      name: "EasyStart Current"
```

Find the MAC by scanning for a name starting with `EasyStart_` — `esp32_ble_tracker`
logs every device it sees, or use `python3 tools/easystart_ble.py scan` from a laptop.

## Entities

| Platform | Key | Notes |
|---|---|---|
| sensor | `live_current` | A |
| sensor | `last_start_peak` | A, peak of the last compressor start |
| sensor | `line_frequency` | Hz, derived as `500000 / period_counter` |
| sensor | `learned_starts` | |
| sensor | `total_starts` | lifetime counter |
| sensor | `total_faults` | lifetime counter |
| sensor | `scpt_remaining` | seconds left on the short-cycle lockout |
| sensor | `state_code` | raw 0–9 state, if you want to template on it |
| text_sensor | `system_state` | decoded state text |
| text_sensor | `model` | e.g. "399 - Breeze", from EEPROM |
| text_sensor | `firmware` | from EEPROM |
| binary_sensor | `fault` | true when state is not Normal or Short Cycle Delay |

All keys are optional — declare only what you want.

## Two units on one ESP32

Supported and verified working - two EasyStarts polled concurrently at 10s with
no contention. See `easystart-dual.yaml`. ESPHome allows 3 concurrent BLE
connections by default, so the limit is RF range, not the ESP32: the node needs
usable RSSI to both units, which in practice means they sit near each other.

That example also adds a per-client `ble_client` RSSI sensor for each unit,
which is the practical way to choose a mounting spot - watch both numbers while
moving the node. Aim for better than -75 dBm; around -85 a ~1KB EEPROM read
(roughly 50 notifications at the negotiated MTU of 23) starts risking a retry.

## When a unit is powered off

The EasyStart is powered from the compressor circuit, so it stops advertising
whenever the A/C is off. `ble_client` reconnects on its own when it returns.
Meanwhile:

| Entity | State |
|---|---|
| `current` | `0` A - the unit is drawing nothing |
| `last_start_peak` | **retains** its last value |
| other numeric sensors | `unknown` (the component publishes NaN) |
| `system_state` | the string `Disconnected` |
| `ble_client` RSSI | `unknown`, plus a warning each poll |
| `model`, `firmware` | retain their last value (static identity) |
| `fault` | **retains** its last value |

`current` reports 0 because that is physically what an unpowered unit draws, and
it keeps the HA history graph continuous. `last_start_peak` describes the last
start the unit performed, which is still the last start once it powers down.

ESPHome binary sensors have no unknown state, so `fault` stays at whatever it
last published. Gate automations on `system_state != 'Disconnected'` rather
than trusting `fault` on its own.

## Behaviour notes

- **The EasyStart is powered from the compressor circuit.** It only advertises
  while the A/C has power. `ble_client` reconnects by itself; on disconnect the
  sensors go to the states in the table above rather than going stale.
- **Model and firmware come from a 1100-byte EEPROM dump**, read once per
  connection. That read has no length header and no chunk sequence numbers, so
  the component validates the length and the ASCII board code before trusting it
  and retries on the next poll if it looks wrong.
- **`update_interval` only controls the live-data poll.** The vendor app uses 5s;
  10s is a gentler default.
- **No authentication.** These devices have no pairing, bonding, or password.
- Writing settings (`SMask` / `FMask` / `SCPT`) is documented in the protocol
  notes but deliberately **not implemented here** — this component is read-only.
