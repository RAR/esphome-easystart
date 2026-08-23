"""
Reference BLE client for Micro-Air EasyStart soft starters.

Protocol reverse-engineered from net.microair.easystart 4.3; see
easystart-ble-protocol.md for the full write-up.

There is no authentication on these devices — no pairing, no password.

Requires: pip install bleak

    python3 easystart_ble.py scan
    python3 easystart_ble.py live   AA:BB:CC:DD:EE:FF
    python3 easystart_ble.py info   AA:BB:CC:DD:EE:FF
    python3 easystart_ble.py watch  AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from collections.abc import Awaitable, Callable
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "d973f2e0-b19e-11e2-9e96-0800200c9a66"
CHAR_NOTIFY = "d973f2e1-b19e-11e2-9e96-0800200c9a66"  # device -> host
CHAR_WRITE = "d973f2e2-b19e-11e2-9e96-0800200c9a66"   # host -> device

NAME_MATCH = "EasyStart_"

LIVE_LEN = 20
EEP_LEN = 1100
READ_TIMEOUT = 20.0  # matches the app's Connect_State_CommTO
POLL_INTERVAL = 5.0  # matches the app's Status screen timer

# EEPROM offsets, from MainActivityKt's ESdataIndex* constants.
EEP_MODEL = slice(2, 9)
EEP_FW_VER = 10
EEP_SMASK = 906
EEP_FMASK = 907
EEP_SCPT = 908

# Status.statusText
SYSTEM_STATE = (
    "Normal",
    "Unexpctd Curr Flt",
    "Short Cycle Delay",
    "Pwr Intrrptn Fault",
    "Stall Fault",
    "Stuck SR Fault",
    "Open Ovrld Fault",
    "Overcurrent Fault",
    "Bad Wiring Fault",
    "Wrong Voltage Flt",
)

# EEPROM[906], bit -> name. Bits 3/4 are the app's hidden features.
STARTUP_FLAGS = (
    "relearn",
    "use_default_ramp",
    "no_power_up_delay",
    "start_delay_mode",
    "super_learn",
)

# EEPROM[907], bit -> fault whose detection the bit ENABLES.
FAULT_FLAGS = (
    "unexpected_current",
    "power_interruption",
    "compressor_stall",
    "start_hw_failed",
    "open_overload",
    "overcurrent",
    "wiring_issue",
)

MODELS = {
    "364ULBT": "364 - Legacy",
    "368ULBT": "368 - Legacy",
    "398ULBT": "398 - Flex",
    "399BT": "399 - Breeze",
}


class EasyStartError(Exception):
    pass


class CommandFailed(EasyStartError):
    """Device replied with a 'Fail' terminator."""


@dataclass
class LiveData:
    """Decoded ReadLive block (Status.onCreateView$lambda$2)."""

    state_code: int = 0
    state: str = ""
    learned_starts: int = 0
    live_current: float = 0.0        # amps
    line_frequency: float | None = None  # Hz, None if the period counter is 0
    last_start_peak: float = 0.0     # amps
    scpt_delay_remaining: int = 0    # seconds
    total_faults: int = 0
    total_starts: int = 0
    raw: str = ""

    @property
    def fault(self) -> bool:
        # State 2 is a normal short-cycle delay, not a fault.
        return self.state_code not in (0, 2)

    @classmethod
    def parse(cls, buf: bytes) -> "LiveData":
        if len(buf) < 18:
            raise EasyStartError(f"live block too short: {len(buf)} bytes")
        code = buf[2]
        period = int.from_bytes(buf[6:8], "little")
        return cls(
            state_code=code,
            state=SYSTEM_STATE[code] if code < len(SYSTEM_STATE) else "Not Defined",
            learned_starts=buf[3],
            live_current=int.from_bytes(buf[4:6], "little") / 10.0,
            line_frequency=(500000.0 / period) if period else None,
            last_start_peak=int.from_bytes(buf[8:10], "little") / 10.0,
            scpt_delay_remaining=int.from_bytes(buf[10:12], "little"),
            total_faults=int.from_bytes(buf[12:14], "little"),
            total_starts=int.from_bytes(buf[14:18], "little"),
            raw=buf.hex(),
        )


@dataclass
class DeviceInfo:
    """The four fields the app decodes out of the 1100-byte EEPROM image."""

    model_code: str = ""
    model: str = ""
    firmware: int = 0
    startup_mask: int = 0
    startup_flags: dict[str, bool] = field(default_factory=dict)
    normal_operation: bool = False
    fault_mask: int = 0
    fault_flags: dict[str, bool] = field(default_factory=dict)
    scpt_minutes: int = 0

    @classmethod
    def parse(cls, eep: bytes) -> "DeviceInfo":
        if len(eep) <= EEP_SCPT:
            raise EasyStartError(f"EEPROM image too short: {len(eep)} bytes")
        code = eep[EEP_MODEL].decode("ascii", "replace").rstrip("\x00\xff ")
        smask, fmask = eep[EEP_SMASK], eep[EEP_FMASK]
        return cls(
            model_code=code,
            model=next((v for k, v in MODELS.items() if k in code), "Unknown"),
            firmware=eep[EEP_FW_VER],
            startup_mask=smask,
            startup_flags={n: bool(smask & (1 << i)) for i, n in enumerate(STARTUP_FLAGS)},
            normal_operation=(smask & 7) == 0,
            fault_mask=fmask,
            fault_flags={n: bool(fmask & (1 << i)) for i, n in enumerate(FAULT_FLAGS)},
            scpt_minutes=eep[EEP_SCPT],
        )


async def discover(timeout: float = 10.0) -> list[BLEDevice]:
    """Scan for advertising EasyStart devices."""
    found = await BleakScanner.discover(timeout=timeout)
    return [d for d in found if d.name and NAME_MATCH in d.name]


class EasyStartClient:
    """Talks the EasyStart BLE UART protocol.

    A read is: write the command, collect binary notifications, stop at the
    ASCII terminator containing "Success" or "Fail".
    """

    def __init__(
        self,
        address_or_device: str | BLEDevice,
        connector: Callable[[str | BLEDevice], Awaitable[BleakClient]] | None = None,
    ) -> None:
        """address_or_device: a MAC string, or a BLEDevice.

        connector: optional coroutine that takes the target and returns a
        connected BleakClient. Under Home Assistant pass one built on
        bleak_retry_connector.establish_connection so the connection is routed
        through an ESPHome Bluetooth proxy and retried on slot contention.
        """
        self._target = address_or_device
        self._connector = connector
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._buffer = bytearray()
        self._done: asyncio.Future[str] | None = None

    async def __aenter__(self) -> "EasyStartClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        if self._connector is not None:
            client = await self._connector(self._target)
        else:
            client = BleakClient(self._target)
            await client.connect()
        self._client = client
        # bleak writes the CCCD for us. The app additionally requests MTU 517;
        # BlueZ negotiates automatically, other backends may expose a knob.
        await client.start_notify(CHAR_NOTIFY, self._on_notify)

    async def disconnect(self) -> None:
        if self.is_connected:
            assert self._client is not None
            await self._client.disconnect()
        self._client = None

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        # Terminator packets are ASCII and contain "Success" or "Fail";
        # everything else is raw payload.
        text = bytes(data).decode("utf-8", "replace")
        if "Success" in text or "Fail" in text:
            if self._done is not None and not self._done.done():
                self._done.set_result(text)
            return
        self._buffer.extend(data)

    async def _command(
        self, cmd: str, expect_data: bool, timeout: float = READ_TIMEOUT
    ) -> bytes:
        if not self.is_connected:
            raise EasyStartError("not connected")
        assert self._client is not None

        async with self._lock:
            self._buffer.clear()
            loop = asyncio.get_running_loop()
            self._done = loop.create_future()
            try:
                # Note: the payload is the literal pseudo-JSON string, unquoted
                # value and all. Do not run it through json.dumps.
                await self._client.write_gatt_char(CHAR_WRITE, cmd.encode("ascii"))
                terminator = await asyncio.wait_for(self._done, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise EasyStartError(f"timeout waiting for reply to {cmd}") from exc
            finally:
                self._done = None

            if "Fail" in terminator:
                raise CommandFailed(f"{cmd} -> {terminator.strip()!r}")
            _LOGGER.debug("%s -> %d bytes, %r", cmd, len(self._buffer), terminator.strip())
            if expect_data and not self._buffer:
                raise EasyStartError(f"{cmd} returned no data")
            return bytes(self._buffer)

    # -- reads ---------------------------------------------------------------

    async def read_live(self) -> LiveData:
        return LiveData.parse(await self._command('{"Cmd": ReadLive}', expect_data=True))

    async def read_eeprom(self, retries: int = 2) -> bytes:
        """The full 1100-byte image. Slow — cache it.

        The protocol has no length header and no per-chunk sequence numbers, so
        a dropped notification would silently truncate or corrupt the image.
        That is unlikely over a local adapter but a real risk over an ESPHome
        Bluetooth proxy, so validate and retry rather than trusting the buffer.
        """
        last = b""
        for attempt in range(retries + 1):
            last = await self._command('{"Cmd": ReadEEP}', expect_data=True)
            if self._eeprom_looks_sane(last):
                return last
            _LOGGER.warning(
                "EEPROM read %d/%d looks corrupt (%d bytes, expected %d) - retrying",
                attempt + 1, retries + 1, len(last), EEP_LEN,
            )
        raise EasyStartError(
            f"EEPROM read failed validation after {retries + 1} attempts "
            f"(last was {len(last)} bytes)"
        )

    @staticmethod
    def _eeprom_looks_sane(eep: bytes) -> bool:
        if len(eep) != EEP_LEN:
            return False
        # Bytes 2-8 are the ASCII board code; anything else means we lost a chunk.
        return all(0x20 <= b < 0x7F or b in (0, 0xFF) for b in eep[EEP_MODEL])

    async def read_info(self) -> DeviceInfo:
        return DeviceInfo.parse(await self.read_eeprom())

    # -- writes --------------------------------------------------------------

    async def _write_setting(self, key: str, value: int) -> None:
        if not 0 <= value <= 0xFF:
            raise ValueError(f"{key} must fit in one byte, got {value}")
        # The device ACKs but does not echo; the app re-reads EEPROM to confirm.
        await self._command(f'{{"Cmd": {key}={value:02X}}}', expect_data=False)

    async def set_startup_mask(self, mask: int) -> DeviceInfo:
        """EEPROM[906]. See STARTUP_FLAGS. Returns the re-read state."""
        await self._write_setting("SMask", mask)
        return await self.read_info()

    async def set_scpt_minutes(self, minutes: int) -> DeviceInfo:
        """EEPROM[908], short-cycle protection delay in minutes (1-250)."""
        if not 1 <= minutes <= 250:
            raise ValueError("SCPT delay must be 1-250 minutes")
        await self._write_setting("SCPT", minutes)
        return await self.read_info()

    async def set_fault_mask(self, mask: int) -> DeviceInfo:
        """EEPROM[907]. Each bit ENABLES a fault detector — see FAULT_FLAGS.

        Clearing bits disables protection on a compressor soft starter. The
        vendor app requires an explicit confirmation before doing this.
        """
        await self._write_setting("FMask", mask)
        return await self.read_info()


def build_mask(flags: dict[str, bool], names: tuple[str, ...]) -> int:
    """Helper: turn a {flag_name: bool} dict back into a mask byte."""
    unknown = set(flags) - set(names)
    if unknown:
        raise ValueError(f"unknown flags: {sorted(unknown)}")
    return sum(1 << i for i, n in enumerate(names) if flags.get(n))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _cmd_scan(args: argparse.Namespace) -> None:
    devices = await discover(args.timeout)
    if not devices:
        print("No EasyStart devices found.")
        return
    for d in devices:
        print(f"{d.address}  {d.name}")


async def _cmd_live(args: argparse.Namespace) -> None:
    async with EasyStartClient(args.address) as client:
        print(json.dumps(asdict(await client.read_live()), indent=2))


async def _cmd_info(args: argparse.Namespace) -> None:
    async with EasyStartClient(args.address) as client:
        info = await client.read_info()
        print(json.dumps(asdict(info), indent=2))


async def _cmd_watch(args: argparse.Namespace) -> None:
    async with EasyStartClient(args.address) as client:
        info = await client.read_info()
        print(f"{info.model} (fw {info.firmware}), SCPT {info.scpt_minutes} min\n")
        while True:
            live = await client.read_live()
            freq = f"{live.line_frequency:.1f}" if live.line_frequency else "--"
            print(
                f"{live.state:<20} {live.live_current:6.1f} A  "
                f"peak {live.last_start_peak:6.1f} A  {freq:>5} Hz  "
                f"starts {live.total_starts}  faults {live.total_faults}"
            )
            await asyncio.sleep(args.interval)


async def _cmd_dump(args: argparse.Namespace) -> None:
    async with EasyStartClient(args.address) as client:
        eep = await client.read_eeprom()
    with open(args.out, "wb") as fh:
        fh.write(eep)
    print(f"wrote {len(eep)} bytes to {args.out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="find EasyStart devices")
    s.add_argument("--timeout", type=float, default=10.0)
    s.set_defaults(func=_cmd_scan)

    s = sub.add_parser("live", help="one live-data read")
    s.add_argument("address")
    s.set_defaults(func=_cmd_live)

    s = sub.add_parser("info", help="model, firmware and settings from EEPROM")
    s.add_argument("address")
    s.set_defaults(func=_cmd_info)

    s = sub.add_parser("watch", help="poll live data continuously")
    s.add_argument("address")
    s.add_argument("--interval", type=float, default=POLL_INTERVAL)
    s.set_defaults(func=_cmd_watch)

    s = sub.add_parser("dump", help="save the raw 1100-byte EEPROM image")
    s.add_argument("address")
    s.add_argument("--out", default="easystart.eep")
    s.set_defaults(func=_cmd_dump)

    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
