#include "easystart.h"

#ifdef USE_ESP32

#include "esphome/core/log.h"
#include <cstring>

namespace esphome {
namespace easystart {

static const char *const TAG = "easystart";

// ESPBTUUID::from_raw parses the canonical 36-char hyphenated form directly.
const char *const SERVICE_UUID = "d973f2e0-b19e-11e2-9e96-0800200c9a66";
const char *const NOTIFY_UUID = "d973f2e1-b19e-11e2-9e96-0800200c9a66";
const char *const WRITE_UUID = "d973f2e2-b19e-11e2-9e96-0800200c9a66";

// Status.statusText in the vendor app.
static const char *const SYSTEM_STATE[] = {
    "Normal",           "Unexpctd Curr Flt", "Short Cycle Delay", "Pwr Intrrptn Fault", "Stall Fault",
    "Stuck SR Fault",   "Open Ovrld Fault",  "Overcurrent Fault", "Bad Wiring Fault",   "Wrong Voltage Flt",
};
static const uint8_t SYSTEM_STATE_COUNT = sizeof(SYSTEM_STATE) / sizeof(SYSTEM_STATE[0]);

static uint16_t le16(const uint8_t *p) { return (uint16_t) p[0] | ((uint16_t) p[1] << 8); }
static uint32_t le32(const uint8_t *p) {
  return (uint32_t) p[0] | ((uint32_t) p[1] << 8) | ((uint32_t) p[2] << 16) | ((uint32_t) p[3] << 24);
}

void EasyStart::setup() { this->mark_unavailable_(); }

void EasyStart::dump_config() {
  ESP_LOGCONFIG(TAG, "Micro-Air EasyStart:");
  LOG_UPDATE_INTERVAL(this);
}

void EasyStart::update() {
  if (this->node_state != espbt::ClientState::ESTABLISHED) {
    // Only powered while the A/C circuit is.
    return;
  }
  if (this->pending_ != Pending::NONE) {
    ESP_LOGD(TAG, "Previous read still in flight, skipping this poll");
    return;
  }
  // Static info: read once per connection.
  if (!this->eeprom_read_done_) {
    this->send_command_("{\"Cmd\": ReadEEP}", Pending::EEP);
    return;
  }
  this->send_command_("{\"Cmd\": ReadLive}", Pending::LIVE);
}

void EasyStart::loop() {
  if (this->pending_ == Pending::NONE)
    return;
  if (millis() < this->deadline_)
    return;
  ESP_LOGW(TAG, "Timed out after %ums with %u bytes buffered", READ_TIMEOUT_MS, this->buffer_len_);
  this->finish_(false);
}

bool EasyStart::send_command_(const char *cmd, Pending kind) {
  if (this->write_handle_ == 0) {
    ESP_LOGW(TAG, "Write characteristic not resolved");
    return false;
  }
  this->buffer_len_ = 0;
  this->pending_ = kind;
  this->deadline_ = millis() + READ_TIMEOUT_MS;

  // Vendor pseudo-JSON: the value is unquoted, so send the exact bytes.
  auto status = esp_ble_gattc_write_char(this->parent()->get_gattc_if(), this->parent()->get_conn_id(),
                                         this->write_handle_, (uint16_t) strlen(cmd), (uint8_t *) cmd,
                                         this->write_type_, ESP_GATT_AUTH_REQ_NONE);
  if (status != ESP_OK) {
    ESP_LOGW(TAG, "Write of %s failed, status=%d", cmd, status);
    this->finish_(false);
    return false;
  }
  ESP_LOGV(TAG, "Sent %s", cmd);
  return true;
}

void EasyStart::handle_notify_(const uint8_t *data, uint16_t len) {
  if (this->pending_ == Pending::NONE || len == 0)
    return;

  // The "Success"/"Fail" marker may arrive alone or appended to a data packet,
  // so scan the whole payload and keep any bytes preceding it.
  int marker = -1;
  bool ok = false;
  for (uint16_t i = 0; i < len; i++) {
    if (len - i >= 7 && memcmp(data + i, "Success", 7) == 0) {
      marker = (int) i;
      ok = true;
      break;
    }
    if (len - i >= 4 && memcmp(data + i, "Fail", 4) == 0) {
      marker = (int) i;
      ok = false;
      break;
    }
  }

  uint16_t payload = (marker >= 0) ? (uint16_t) marker : len;
  if (payload > 0) {
    // One buffer for both transfer types; length is validated when parsing.
    if (this->buffer_len_ + payload > EEP_LEN) {
      ESP_LOGW(TAG, "Overflow: %u + %u > %u, discarding read", this->buffer_len_, payload, EEP_LEN);
      this->finish_(false);
      return;
    }
    memcpy(this->buffer_ + this->buffer_len_, data, payload);
    this->buffer_len_ += payload;
  }

  if (marker >= 0) {
    if (!ok)
      ESP_LOGW(TAG, "Device reported failure after %u bytes", this->buffer_len_);
    this->finish_(ok);
  }
}

void EasyStart::trim_status_tail_() {
  // Reads end with an ASCII status reply like {"Sts": "Success"}. At MTU 23 its
  // leading bytes arrive before the marker packet and land in the data buffer.
  // Tail-only and printable-only, so a stray {" inside binary data is safe.
  const uint16_t MAX_TAIL = 48;
  uint16_t start = this->buffer_len_ > MAX_TAIL ? this->buffer_len_ - MAX_TAIL : 0;
  for (uint16_t i = start; i + 1 < this->buffer_len_; i++) {
    if (this->buffer_[i] != '{' || this->buffer_[i + 1] != '"')
      continue;
    bool printable = true;
    for (uint16_t j = i; j < this->buffer_len_; j++) {
      uint8_t ch = this->buffer_[j];
      if (ch < 0x20 || ch > 0x7E) {
        printable = false;
        break;
      }
    }
    if (printable) {
      ESP_LOGV(TAG, "Trimmed %u trailing status bytes", this->buffer_len_ - i);
      this->buffer_len_ = i;
      return;
    }
  }
}

void EasyStart::finish_(bool success) {
  auto kind = this->pending_;
  this->pending_ = Pending::NONE;

  if (success)
    this->trim_status_tail_();

  if (!success) {
    if (kind == Pending::LIVE)
      this->mark_unavailable_();
    return;
  }
  if (kind == Pending::LIVE) {
    this->publish_live_();
  } else if (kind == Pending::EEP) {
    this->publish_eeprom_();
  }
}

void EasyStart::publish_live_() {
  if (this->buffer_len_ < 18) {
    ESP_LOGW(TAG, "Live block too short: %u bytes", this->buffer_len_);
    this->mark_unavailable_();
    return;
  }
  ESP_LOGV(TAG, "Live block %u bytes: %s", this->buffer_len_,
           format_hex_pretty(this->buffer_, this->buffer_len_).c_str());

  const uint8_t *b = this->buffer_;
  uint8_t code = b[2];
  uint16_t period = le16(b + 6);

#ifdef USE_SENSOR
  if (this->state_code_ != nullptr)
    this->state_code_->publish_state(code);
  if (this->learned_starts_ != nullptr)
    this->learned_starts_->publish_state(b[3]);
  if (this->live_current_ != nullptr)
    this->live_current_->publish_state(le16(b + 4) / 10.0f);
  if (this->line_frequency_ != nullptr)
    this->line_frequency_->publish_state(period ? 500000.0f / period : NAN);
  if (this->last_start_peak_ != nullptr)
    this->last_start_peak_->publish_state(le16(b + 8) / 10.0f);
  if (this->scpt_remaining_ != nullptr)
    this->scpt_remaining_->publish_state(le16(b + 10));
  if (this->total_faults_ != nullptr)
    this->total_faults_->publish_state(le16(b + 12));
  if (this->total_starts_ != nullptr)
    this->total_starts_->publish_state(le32(b + 14));
#endif
#ifdef USE_TEXT_SENSOR
  if (this->system_state_ != nullptr)
    this->system_state_->publish_state(code < SYSTEM_STATE_COUNT ? SYSTEM_STATE[code] : "Not Defined");
#endif
#ifdef USE_BINARY_SENSOR
  // State 2 is a normal short-cycle delay, not a fault.
  if (this->fault_ != nullptr)
    this->fault_->publish_state(code != 0 && code != 2);
#endif
}

bool EasyStart::eeprom_looks_sane_() const {
  // Length varies by model (963 and 1023 seen); the app's 1100 is headroom.
  // Require only enough to cover the fields read.
  if (this->buffer_len_ <= EEP_SCPT || this->buffer_len_ > EEP_LEN)
    return false;
  // Bytes 2-8 are the ASCII board code; junk there means we lost a chunk.
  for (uint16_t i = EEP_MODEL_OFF; i < EEP_MODEL_OFF + EEP_MODEL_LEN; i++) {
    uint8_t c = this->buffer_[i];
    if (!((c >= 0x20 && c < 0x7F) || c == 0x00 || c == 0xFF))
      return false;
  }
  return true;
}

void EasyStart::publish_eeprom_() {
  // No length header or chunk sequencing, so a dropped packet corrupts silently.
  if (!this->eeprom_looks_sane_()) {
    ESP_LOGW(TAG, "EEPROM read failed validation (%u bytes), will retry next poll", this->buffer_len_);
    return;
  }
  this->eeprom_read_done_ = true;

  char code[EEP_MODEL_LEN + 1];
  uint8_t n = 0;
  for (uint8_t i = 0; i < EEP_MODEL_LEN; i++) {
    uint8_t c = this->buffer_[EEP_MODEL_OFF + i];
    if (c < 0x20 || c >= 0x7F)
      break;
    code[n++] = (char) c;
  }
  code[n] = '\0';

  const char *model = "Unknown";
  if (strstr(code, "364ULBT") != nullptr) {
    model = "364 - Legacy";
  } else if (strstr(code, "368ULBT") != nullptr) {
    model = "368 - Legacy";
  } else if (strstr(code, "398ULBT") != nullptr) {
    model = "398 - Flex";
  } else if (strstr(code, "399BT") != nullptr) {
    model = "399 - Breeze";
  }

  ESP_LOGI(TAG, "EEPROM %u bytes; Board %s (%s), fw %u, SMask 0x%02X, FMask 0x%02X, SCPT %u min",
           this->buffer_len_, code, model,
           this->buffer_[EEP_FW_VER], this->buffer_[EEP_SMASK], this->buffer_[EEP_FMASK], this->buffer_[EEP_SCPT]);

#ifdef USE_TEXT_SENSOR
  if (this->model_ != nullptr)
    this->model_->publish_state(model);
  if (this->firmware_ != nullptr)
    this->firmware_->publish_state(to_string(this->buffer_[EEP_FW_VER]));
#endif
}

void EasyStart::mark_unavailable_() {
#ifdef USE_SENSOR
  // A unit that is off draws no current and has no line to measure, so report
  // zero for both rather than leaving a gap in the history.
  sensor::Sensor *zero[] = {this->live_current_, this->line_frequency_};
  for (auto *s : zero) {
    if (s != nullptr)
      s->publish_state(0.0f);
  }
  // last_start_peak_ is deliberately absent: it describes the last start, which
  // is still true after the unit powers down.
  sensor::Sensor *unknown[] = {this->learned_starts_, this->total_starts_, this->total_faults_,
                               this->scpt_remaining_, this->state_code_};
  for (auto *s : unknown) {
    if (s != nullptr)
      s->publish_state(NAN);
  }
#endif
#ifdef USE_TEXT_SENSOR
  if (this->system_state_ != nullptr)
    this->system_state_->publish_state("Disconnected");
#endif
}

void EasyStart::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                                    esp_ble_gattc_cb_param_t *param) {
  switch (event) {
    case ESP_GATTC_DISCONNECT_EVT: {
      ESP_LOGD(TAG, "Disconnected");
      this->node_state = espbt::ClientState::IDLE;
      this->pending_ = Pending::NONE;
      this->buffer_len_ = 0;
      this->notify_handle_ = 0;
      this->write_handle_ = 0;
      // Re-read the EEPROM on the next connection - it may be a different unit.
      this->eeprom_read_done_ = false;
      this->mark_unavailable_();
      break;
    }
    case ESP_GATTC_SEARCH_CMPL_EVT: {
      auto service = espbt::ESPBTUUID::from_raw(SERVICE_UUID);
      auto *notify_chr = this->parent()->get_characteristic(service, espbt::ESPBTUUID::from_raw(NOTIFY_UUID));
      auto *write_chr = this->parent()->get_characteristic(service, espbt::ESPBTUUID::from_raw(WRITE_UUID));
      if (notify_chr == nullptr || write_chr == nullptr) {
        ESP_LOGE(TAG, "EasyStart UART service not found on this device");
        break;
      }
      this->notify_handle_ = notify_chr->handle;
      this->write_handle_ = write_chr->handle;
      // Capture the write type now, while the characteristic object is alive.
      this->write_type_ = (write_chr->properties & ESP_GATT_CHAR_PROP_BIT_WRITE)
                              ? ESP_GATT_WRITE_TYPE_RSP
                              : ESP_GATT_WRITE_TYPE_NO_RSP;

      // register_for_notify writes the CCCD for us.
      auto status = esp_ble_gattc_register_for_notify(this->parent()->get_gattc_if(),
                                                      this->parent()->get_remote_bda(), this->notify_handle_);
      if (status) {
        ESP_LOGW(TAG, "register_for_notify failed, status=%d", status);
      }
      break;
    }
    case ESP_GATTC_REG_FOR_NOTIFY_EVT: {
      if (param->reg_for_notify.handle != this->notify_handle_)
        break;
      if (param->reg_for_notify.status != ESP_GATT_OK) {
        ESP_LOGW(TAG, "register_for_notify error, status=%d", param->reg_for_notify.status);
        break;
      }
      this->node_state = espbt::ClientState::ESTABLISHED;
      ESP_LOGD(TAG, "Connected and subscribed");
      // Grab the static info straight away rather than waiting a full interval.
      this->update();
      break;
    }
    case ESP_GATTC_NOTIFY_EVT: {
      if (param->notify.handle != this->notify_handle_)
        break;
      this->handle_notify_(param->notify.value, param->notify.value_len);
      break;
    }
    default:
      break;
  }
}

}  // namespace easystart
}  // namespace esphome

#endif  // USE_ESP32
