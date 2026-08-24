# Micro-Air EasyStart — BLE protocol

Reverse-engineered from `EasyStart_4.3_APKPure.apk`
(`net.microair.easystart` v4.3, single `classes.dex`). The app is **not
obfuscated** — real Kotlin class, method and field names survive, so this
mapping is direct rather than inferred.

> This is a *different app and a different protocol* from Micro-Air Connect
> (`com.microair.connect`), which uses JSON over a `000000FF` service. Nothing
> from that write-up applies here.

Sources:

| What | Class |
|---|---|
| UUIDs, scan + GATT callbacks, RX buffers | `net.microair.easystart.MainActivityKt` |
| Connect state machine, EEPROM read | `Connect` |
| Live data poll + decode | `Status` |
| Startup mask / SCPT settings | `Relearn` |
| Fault mask settings | `Faults` |
| EEPROM upload to Micro-Air | `Diagnose` |
| Firmware OTA | `Update` |

---

> **Hardware-verified 2026-08-23** against two live units:
> `EasyStart_1421` (368ULBT "368 - Legacy", fw 29) and
> `EasyStart_F488` (398ULBT "398 - Flex", fw 36), both read concurrently by a
> single ESP32-S3. Items marked *(confirmed)* were observed on the wire;
> everything else is from the APK.

## 1. Transport

RedBearLab-style BLE UART service — one write characteristic, one notify
characteristic, no read characteristic.

```
Service   d973f2e0-b19e-11e2-9e96-0800200c9a66   (ESPrimaryServiceUUID)
  Notify  d973f2e1-b19e-11e2-9e96-0800200c9a66   (ESNotifyServiceUUID)  device -> app
  Write   d973f2e2-b19e-11e2-9e96-0800200c9a66   (ESWriteServiceUUID)   app -> device
```

**There is no authentication, no pairing, no bonding and no password.** The app
requests no PIN and never calls `createBond()`. Anything in radio range that
speaks this protocol can read and write settings.

### Advertised name

`EasyStart_XXXX` — the scan filter is a substring match on `"EasyStart_"`
(`MainActivityKt$scanCallBack$1`). The app stores up to three device names in
prefs and accepts names of length 14 (`EasyStart_` + 4 chars) or 10
(`EasyStart_` bare).

### Connect sequence (`Connect.connectSM`)

1. Scan until a name containing `EasyStart_` appears (app gives up after 5 s).
2. `connectGatt()`.
3. On connect, **`requestMtu(517)`** — this matters: EEPROM dumps are 1100 bytes
   and the app relies on large notifications.
4. After `onMtuChanged`, `discoverServices()`.
5. Find the primary service, cache both characteristics, `setCharacteristicNotification(notify, true)`
   **and write `ENABLE_NOTIFICATION_VALUE` to every descriptor** on the notify
   characteristic (the app iterates all descriptors rather than looking up 0x2902).
6. Wait 500 ms, then send `{"Cmd": ReadEEP}` to prime the EEPROM image.

No handshake beyond that. Read/write commands can be issued immediately.

---

## 2. Framing

Commands are **pseudo-JSON** — note the unquoted value, this is not valid JSON
and must be sent as the exact literal byte string:

```
{"Cmd": ReadLive}
{"Cmd": SMask=1F}
```

Responses arrive as notifications on `d973f2e1` and come in two flavours,
distinguished in `MainActivityKt$gattCallBack$1.onCharacteristicChanged`:

- **Terminator / status packet** — the payload decoded as text contains
  `"Success"` or `"Fail"`. This ends the current transfer.
- **Data packet** — anything else is raw binary, appended to the buffer selected
  by the current command state.

So a read looks like: *N binary notifications, then one ASCII `…Success…` line.*
There is no length header and no per-chunk sequence number — the client just
concatenates in arrival order until the terminator lands.

> The exact discriminator is slightly obscured by decompilation (jadx loses the
> `||` branch and leaves a dangling `contains("Fail")` test), but the surrounding
> code — every fragment checks `esNotifyStr.contains("Success")` — makes the
> reconstruction unambiguous.

The app tracks which buffer to fill in `esCmdState`:

| `esCmdState` | Meaning | Buffer |
|---:|---|---|
| 0 | `ReadLive` in progress | `esNotifyLiveData`, 20 bytes |
| 1 | `ReadEEP` in progress | `esNotifyEEPData`, 1100 bytes |
| 2 | generic setting write in progress | none (expects terminator only) |
| 3 | `ReadEEP` re-read after a write | `esNotifyEEPData` |
| 4 | OTA / flash | `esNotifyBytes`, 4 bytes |

Timeout for a read is **20 s** (`Connect_State_CommTO`).

---

## 3. Commands

| Command | Purpose |
|---|---|
| `{"Cmd": ReadLive}` | request the 20-byte live-data block |
| `{"Cmd": ReadEEP}` | dump the 1100-byte EEPROM image |
| `{"Cmd": SMask=XX}` | write startup mask, `XX` = uppercase hex, `%02X` |
| `{"Cmd": SCPT=XX}` | write short-cycle-protection delay, `%02X`, valid 1–250 |
| `{"Cmd": FMask=XX}` | write fault-enable mask, `%02X` |

Firmware-update commands (`Update`) — **destructive, documented for completeness
only**: `ProgMode`, `NormMode`, `ChipErase`, `WrtFsL=`, `WrtFsH=`, `WrtFsE=`,
`WrtLkB=`, `OtaPrep`, `OtaBegin`, `OtaWrt`, `OtaEnd`, `OtaAbort`, plus the
`{"Wrt": FlashPg=…}`, `{"Wrt": FlashBuff}`, `{"Int": FlashBuff}`,
`{"End": FlashBuff}`, `{"Ver": FlashPg=…}` family. These drive an AVR bootloader
over BLE. Do not touch these from an integration.

After a settings write the app does a fresh `ReadEEP` to confirm — the device
does not echo the new value in the ACK.

---

### Confirmed on hardware

- **Negotiated MTU is 23** on both units, i.e. 20-byte notifications. The vendor
  app requests 517 and gets whatever the peripheral grants; do not assume large
  chunks. A ~1 KB EEPROM read arrives as roughly 50 notifications.
- **Advertised address type is PUBLIC**, despite both MACs having the two high
  bits of the first octet set (`E9:59:D3:CE:56:FA`, `EE:7A:AB:EB:5A:23`).
- **A read is terminated by an ASCII JSON status reply**, not a bare word. The
  observed reply begins `{"Sts": ` and the `Success` the app tests for lives
  inside it. At MTU 23 that reply is split across notifications, so its leading
  bytes arrive *before* the packet carrying `Success` and a naive reader appends
  them to the data buffer. Strip a trailing printable run beginning with `{"`.
- **Transfer lengths are not fixed.** The app's buffers (20 and 1100 bytes) are
  allocations, not wire lengths. Observed after trimming the status tail: live
  block **18 bytes**; EEPROM **963 bytes** (368/fw29) and **1023 bytes**
  (398/fw36). Validate "long enough for the fields you read", never an exact
  length.
- **Two units on one ESP32 works.** Both were polled concurrently at 10 s with
  no contention, using two `ble_client` + two component instances.

## 4. Live data block (`ReadLive`)

**18 bytes on the wire** *(confirmed)*; the app allocates 20 and uses 2–17. All multi-byte values are **unsigned
little-endian**. Decoded in `Status.onCreateView$lambda$2`; labels from
`res/values/strings.xml`.

| Offset | Type | Field | Scaling |
|---:|---|---|---|
| 0–1 | — | unused by the app | |
| 2 | u8 | **System state** | index into the state table below; > 9 → "Not Defined" |
| 3 | u8 | **Learned starts** | count |
| 4–5 | u16 LE | **Live current** | ÷ 10 → amps |
| 6–7 | u16 LE | line period counter | **Line frequency = 500000 / value** Hz |
| 8–9 | u16 LE | **Last start peak current** | ÷ 10 → amps |
| 10–11 | u16 LE | **SCPT delay remaining** | seconds |
| 12–13 | u16 LE | **Total faults** | count |
| 14–17 | u32 LE | **Total starts** | count |
| 18–19 | — | unused | |

### System state (`Status.statusText`)

| Value | Text |
|---:|---|
| 0 | Normal |
| 1 | Unexpctd Curr Flt |
| 2 | Short Cycle Delay |
| 3 | Pwr Intrrptn Fault |
| 4 | Stall Fault |
| 5 | Stuck SR Fault |
| 6 | Open Ovrld Fault |
| 7 | Overcurrent Fault |
| 8 | Bad Wiring Fault |
| 9 | Wrong Voltage Flt |
| >9 | Not Defined |

The Status screen polls `ReadLive` on a fixed **5 second** timer and skips a tick
if the previous one has not completed.

---

## 5. EEPROM image (`ReadEEP`)

Length varies by model/firmware *(confirmed: 963 and 1023 bytes)*; the app
allocates 1100 as headroom. It interprets four regions, all well inside even
the smallest image observed:

| Offset | Type | Field |
|---:|---|---|
| 2–8 | 7 bytes ASCII | **Board / model code** |
| 10 | u8 | **Firmware version** (`ESdataIndexFWVer`) |
| 906 | u8 | **Startup mask** (`ESdataIndexSMask`) |
| 907 | u8 | **Fault mask** (`ESdataIndexFMask`) |
| 908 | u8 | **SCPT delay setting**, in *minutes*, 1–250 (`ESdataIndexSCPT`) |

Everything else is uploaded verbatim to Micro-Air by the Diagnose screen as
`easystart.eep` and is not decoded locally.

> Note the unit mismatch: EEPROM 908 is the configured SCPT delay in **minutes**
> (`relearn_label5a = "SCPT (minutes)"`), while live-data bytes 10–11 are the
> remaining delay in **seconds** (`status_label8 = "SCPT Delay (sec)"`).

### Model codes (bytes 2–8)

| Code | Model |
|---|---|
| `364ULBT` | 364 — Legacy |
| `368ULBT` | 368 — Legacy |
| `398ULBT` | 398 — Flex |
| `399BT` | 399 — Breeze |

### Startup mask — EEPROM[906], written with `SMask=`

| Bit | Meaning |
|---:|---|
| 0 | ReLearn |
| 1 | Use Default Ramp |
| 2 | No Power-Up Delay |
| 3 | SCPT field is a *Start Delay* rather than short-cycle protection (relabels the UI) |
| 4 | ReLearn behaves as *SuperLearn* (relabels the UI) |

`mask & 7 == 0` is displayed as "Normal Operation". Bits 3 and 4 are hidden
features — the app only exposes them after a 3-second long-press, and gates them
on firmware ≥ 29 (`EEPROM[10] >= 0x1D`) or the `399BT` board.

### Fault mask — EEPROM[907], written with `FMask=`

Each bit **enables** the corresponding fault detection.

| Bit | Fault |
|---:|---|
| 0 | Unexpected Current |
| 1 | Power Interruption |
| 2 | Compressor Stall |
| 3 | Start H/W Failed (stuck start relay) |
| 4 | Open Overload |
| 5 | Overcurrent |
| 6 | Wiring Issue (no start-winding zero-cross) |

The app makes you confirm a dialog before writing this — disabling fault
detection on a compressor soft starter has real consequences.

---

## 6. Notes for a Home Assistant integration

- **`ReadLive` is all you need for sensors.** One 17-byte poll gives state,
  current, frequency, peak, and the three counters. Match the app's 5 s cadence
  or slower.
- **`ReadEEP` is expensive** (1100 bytes over notifications) — read it once at
  setup for model, firmware and the two masks, and re-read only after a write.
- **MTU matters.** Request the largest MTU your stack allows before reading
  EEPROM. On BlueZ, `bleak` negotiates automatically; if the peripheral ends up
  at MTU 23 the dump becomes ~55 notifications, which still works but is slow.
- **No auth means no lockout** — but also means the link is exclusive. The phone
  app and the integration cannot both be connected.
- **Entity mapping**: `sensor` for live current (A), last start peak (A), line
  frequency (Hz), learned starts, total starts, total faults, SCPT delay
  remaining (s); an enum/`sensor` for system state with a `binary_sensor` problem
  flag for state != 0; diagnostic attributes for model code and firmware version.
- **Writable settings** (`switch` entities off the two masks, `number` for SCPT)
  are possible but I would leave the fault mask read-only by default — the app
  guards it behind a confirmation for good reason.
- As with any BLE device, a **custom integration** on HA's `bluetooth` stack fits
  better than an add-on; an add-on container has no direct access to HA's adapter
  management and would contend for the radio.

---

## 7. Vendor cloud services

None of this is needed by the integration - the ESP32 only ever talks BLE. It is
recorded because it answers "does the app phone home", and because the transport
choices matter if you ever run the vendor app.

### Firmware update check

The app's `Update` tab checks two plain-HTTP files, then downloads the image:

| URL | Contents |
|---|---|
| `http://easystart.microair.net/downloads/registration.txt` | one `EasyStart_XXXX,<version>` line per registered unit; gates whether an update is offered |
| `http://easystart.microair.net/downloads/updates.txt` | one line per board family, fields below |
| `http://easystart.microair.net/downloads/<hexFilename>` | the Intel HEX (or `.bin` for 399BT) pushed over the AVR OTA commands |

`updates.txt` fields, indexed as `Update.updateAvail` reads them:

```
0: board family   (first 3 chars matched against 364/368/398/399)
1: version
2: low fuses      -> {"Cmd": WrtFsL=..}
3: high fuses     -> {"Cmd": WrtFsH=..}
4: ext fuses      -> {"Cmd": WrtFsE=..}
5: lock bits      -> {"Cmd": WrtLkB=..}
6: EEPROM filename
7: HEX filename
```

Live as of 2026-08-24, serving `364ULBT-B29`, `368ULBT-B29`, `398ULBT-B37` and
`399BT-C11-APP.bin`.

The exact registration predicate is hard to state with confidence: jadx mangles
the `readLine` loops in `Update` badly enough that the surrounding comparisons
cannot be trusted. What is clear is that the decision keys on the advertised
device name and a version byte compared against EEPROM[10].

If startup mask bit 2 (*No Power-Up Delay*) is set, the app offers the update but
warns that the unit may power down mid-write and be bricked, and tells you to
call Micro-Air first.

### Diagnostics upload

`Diagnose` POSTs a multipart form to `http://easystart.microair.net/upload.php`
containing customer name, email and phone from the form, the advertised device
name as `easystart_id`, and the EEPROM dump as `easystart.eep`.

### Transport caveat

Every one of these is `http://`, not `https://`. Update metadata, the firmware
image itself, and the diagnostics form (personal details included) all travel in
clear text with no signature check beyond the per-line Intel HEX checksums. An
attacker positioned on the network could serve arbitrary firmware to a soft
starter wired into a compressor. Worth knowing; not something this component
touches.
