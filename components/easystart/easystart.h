#pragma once

#include "esphome/components/ble_client/ble_client.h"
#include "esphome/core/component.h"

#ifdef USE_ESP32

#include <esp_gattc_api.h>

#ifdef USE_SENSOR
#include "esphome/components/sensor/sensor.h"
#endif
#ifdef USE_TEXT_SENSOR
#include "esphome/components/text_sensor/text_sensor.h"
#endif
#ifdef USE_BINARY_SENSOR
#include "esphome/components/binary_sensor/binary_sensor.h"
#endif

namespace esphome {
namespace easystart {

namespace espbt = esphome::esp32_ble_tracker;

// RedBearLab-style BLE UART service used by Micro-Air EasyStart.
extern const char *const SERVICE_UUID;
extern const char *const NOTIFY_UUID;
extern const char *const WRITE_UUID;

static const uint16_t LIVE_LEN = 20;
static const uint16_t EEP_LEN = 1100;

// EEPROM offsets (MainActivityKt ESdataIndex* in the vendor app).
static const uint16_t EEP_MODEL_OFF = 2;
static const uint16_t EEP_MODEL_LEN = 7;
static const uint16_t EEP_FW_VER = 10;
static const uint16_t EEP_SMASK = 906;
static const uint16_t EEP_FMASK = 907;
static const uint16_t EEP_SCPT = 908;

// Reads have no length header and no terminator until the device says so.
static const uint32_t READ_TIMEOUT_MS = 20000;

enum class Pending : uint8_t { NONE, LIVE, EEP };

class EasyStart : public PollingComponent, public ble_client::BLEClientNode {
 public:
  void setup() override;
  void update() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                           esp_ble_gattc_cb_param_t *param) override;

#ifdef USE_SENSOR
  void set_live_current(sensor::Sensor *s) { this->live_current_ = s; }
  void set_last_start_peak(sensor::Sensor *s) { this->last_start_peak_ = s; }
  void set_line_frequency(sensor::Sensor *s) { this->line_frequency_ = s; }
  void set_learned_starts(sensor::Sensor *s) { this->learned_starts_ = s; }
  void set_total_starts(sensor::Sensor *s) { this->total_starts_ = s; }
  void set_total_faults(sensor::Sensor *s) { this->total_faults_ = s; }
  void set_scpt_remaining(sensor::Sensor *s) { this->scpt_remaining_ = s; }
  void set_state_code(sensor::Sensor *s) { this->state_code_ = s; }
#endif
#ifdef USE_TEXT_SENSOR
  void set_system_state(text_sensor::TextSensor *s) { this->system_state_ = s; }
  void set_model(text_sensor::TextSensor *s) { this->model_ = s; }
  void set_firmware(text_sensor::TextSensor *s) { this->firmware_ = s; }
#endif
#ifdef USE_BINARY_SENSOR
  void set_fault(binary_sensor::BinarySensor *s) { this->fault_ = s; }
#endif

 protected:
  bool send_command_(const char *cmd, Pending kind);
  void handle_notify_(const uint8_t *data, uint16_t len);
  void finish_(bool success);
  void publish_live_();
  void publish_eeprom_();
  void mark_unavailable_();
  bool eeprom_looks_sane_() const;
  void trim_status_tail_();

  // Handles only - NEVER cache BLECharacteristic*. Setting node_state to
  // ESTABLISHED lets BLEClientBase free the service/characteristic objects,
  // so a cached pointer dangles on the next write (use-after-free).
  uint16_t notify_handle_{0};
  uint16_t write_handle_{0};
  esp_gatt_write_type_t write_type_{ESP_GATT_WRITE_TYPE_RSP};

  Pending pending_{Pending::NONE};
  uint32_t deadline_{0};
  bool eeprom_read_done_{false};

  // Sized for the larger of the two transfers.
  uint8_t buffer_[EEP_LEN];
  uint16_t buffer_len_{0};

#ifdef USE_SENSOR
  sensor::Sensor *live_current_{nullptr};
  sensor::Sensor *last_start_peak_{nullptr};
  sensor::Sensor *line_frequency_{nullptr};
  sensor::Sensor *learned_starts_{nullptr};
  sensor::Sensor *total_starts_{nullptr};
  sensor::Sensor *total_faults_{nullptr};
  sensor::Sensor *scpt_remaining_{nullptr};
  sensor::Sensor *state_code_{nullptr};
#endif
#ifdef USE_TEXT_SENSOR
  text_sensor::TextSensor *system_state_{nullptr};
  text_sensor::TextSensor *model_{nullptr};
  text_sensor::TextSensor *firmware_{nullptr};
#endif
#ifdef USE_BINARY_SENSOR
  binary_sensor::BinarySensor *fault_{nullptr};
#endif
};

}  // namespace easystart
}  // namespace esphome

#endif  // USE_ESP32
