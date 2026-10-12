"""Manual Thorlabs MFF101 control through pylablib's direct FTDI driver."""
from __future__ import annotations

import math
import logging
import re
import time


_SERIAL_PATTERN = re.compile(r"37[0-9]{6}")
_IO_TIMEOUT_S = 1.0
_POLL_INTERVAL_S = 0.05
_LOG = logging.getLogger(__name__)


def _load_thorlabs():
    # Keep optional hardware dependencies out of ordinary application startup.
    from pylablib.devices import Thorlabs

    return Thorlabs


def scan_mff101_devices() -> list[str]:
    """Return MFF-family serials; confirm the exact model when connecting.

    Thorlabs assigns prefix 37 to MFF10x devices. Enumeration does not open
    candidates, so an MFF102 can appear here and will be rejected on connect.
    """
    devices = _load_thorlabs().list_kinesis_devices()
    return sorted({
        str(serial)
        for serial, _description in devices
        if _SERIAL_PATTERN.fullmatch(str(serial))
    })


class MFF101Adapter:
    """Expose positions 1/2 and confirm a move with hardware readback.

    All methods must run on the same device worker thread. Closing the USB
    handle does not stop a physical flip that has already been commanded.
    """

    def __init__(self, serial_number: str):
        serial = serial_number.strip() if isinstance(serial_number, str) else ""
        if not _SERIAL_PATTERN.fullmatch(serial):
            raise ValueError("MFF101 serial number must be eight digits starting with 37")
        self._serial_number = serial
        self._device = None

        # MFF's constructor uses a finite 3 s communication timeout internally.
        # Its public constructor has no timeout argument; lower it afterward.
        driver = _load_thorlabs().MFF(serial)
        try:
            driver.instr.set_timeout(_IO_TIMEOUT_S)
            info = driver.get_device_info()
            model = str(info.model_no).strip().upper()
            # MFF101 APT firmware can identify itself as MFF002 (e.g. 1.0.3).
            # Its HW_REQ_INFO serial may be zero even though FTDI exposes the
            # real serial. MFF(serial) opens FTDI by that exact USB serial.
            # Keep this exception limited to the observed filter-flipper type.
            legacy_mff = model == "MFF002" and info.hw_type == 16 and info.nchannels == 1
            if model != "MFF101" and not legacy_mff:
                raise RuntimeError(
                    f"Expected MFF101, but device {serial} reports {info.model_no!r}"
                )
            reported_serial = str(info.serial_no).strip()
            if reported_serial != serial and not (legacy_mff and reported_serial == "0"):
                raise RuntimeError(
                    f"Expected serial {serial}, but device reports {info.serial_no!r}"
                )
        except Exception:
            try:
                driver.close()
            except Exception:
                pass  # Preserve the original connection/identity failure.
            raise
        self._device = driver

    @property
    def serial_number(self) -> str:
        return self._serial_number

    def _require_open(self):
        if self._device is None:
            raise RuntimeError("MFF101 is disconnected")
        return self._device

    def get_position(self) -> int | None:
        """Read endpoint 1/2, or return None while the flipper is moving."""
        state = self._require_open().get_state()
        if state is None:
            return None
        if state == 0:
            return 1
        if state == 1:
            return 2
        raise RuntimeError(f"MFF101 returned an unknown position state: {state!r}")

    def move_to(self, position: int, timeout_s: float = 5.0, *, stop_event=None) -> int:
        """Verify arrival, allowing one retry only from the other known endpoint.

        The first command gets ``timeout_s`` before a retry is eligible; the
        whole operation has a ``2 * timeout_s`` deadline, including any wait
        for existing motion. In-transit readbacks never trigger a command.
        Communication failures propagate immediately. Stop ends polling and
        prevents further commands; an issued physical flip cannot be stopped.
        """
        if type(position) is not int or position not in (1, 2):
            raise ValueError("MFF101 position must be 1 or 2")
        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError) as exc:
            raise ValueError("MFF101 move timeout must be a finite positive number") from exc
        if not math.isfinite(2 * timeout) or timeout <= 0:
            raise ValueError("MFF101 move timeout must be a finite positive number")
        driver = self._require_open()
        deadline = time.monotonic() + 2 * timeout
        commands = 0
        retry_after = None

        def check_stop():
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("MFF101 movement stopped; no further command was sent.")

        def remaining_time():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"MFF101 did not reach position {position} within {2 * timeout:g} s "
                    f"after {commands} command(s); position is unverified."
                )
            return remaining

        while True:
            check_stop()
            with driver.instr.using_timeout(min(_IO_TIMEOUT_S, remaining_time())):
                actual = self.get_position()
            check_stop()
            remaining_time()
            if actual == position:
                return actual
            if actual in (1, 2) and (commands == 0 or
                    (commands == 1 and time.monotonic() >= retry_after)):
                check_stop()
                if commands == 1:
                    _LOG.warning("MFF101 %s still at position %s; retrying position %s (2/2)",
                                 self._serial_number, actual, position)
                with driver.instr.using_timeout(min(_IO_TIMEOUT_S, remaining_time())):
                    driver.move_to_state(position - 1)
                commands += 1
                if commands == 1:
                    retry_after = time.monotonic() + timeout
            time.sleep(min(_POLL_INTERVAL_S, remaining_time()))

    def close(self) -> None:
        """Release the FTDI connection without issuing any motion command."""
        driver = self._device
        if driver is not None:
            driver.close()
            self._device = None
