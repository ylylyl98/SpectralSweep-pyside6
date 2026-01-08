from __future__ import annotations
from typing import Optional
import pyvisa

class NewportEPS300:
    """
    EPS300 rotation controller via VISA.
    Works with:
      - RS-232: e.g. "ASRL4::INSTR" or "ASRL4::INSTR@9600,N,8,1"
      - GPIB:   e.g. "GPIB0::5::INSTR"
    You can pass a raw VISA resource string and we auto-configure sensible defaults.
    """
    def __init__(self,
                 resource: str,
                 rm: Optional[pyvisa.ResourceManager] = None,
                 timeout_ms: int = 5000,
                 read_term: str = "\n",
                 write_term: str = "\r\n",
                 baud: Optional[int] = None):
        self.rm = rm or pyvisa.ResourceManager()
        self.inst = self.rm.open_resource(resource)
        self.inst.timeout = timeout_ms
        # Set terminations (EPS300 accepts LF or CRLF; CRLF is safest for write)
        self.inst.read_termination = read_term
        self.inst.write_termination = write_term

        # If SERIAL (ASRL...), optionally configure baud etc.
        if resource.upper().startswith("ASRL"):
            # VISA usually lets you set attributes on the opened serial session.
            if baud is not None:
                try:
                    self.inst.baud_rate = int(baud)
                except Exception:
                    pass
            # You can also set parity, stop bits, data bits here if needed:
            # self.inst.parity = pyvisa.constants.Parity.none
            # self.inst.stop_bits = pyvisa.constants.StopBits.one
            # self.inst.data_bits = 8

        # Clear interface / buffer
        try:
            self.inst.clear()
        except Exception:
            pass

    # ---------- Device I/O ----------
    def _cmd(self, s: str) -> None:
        self.inst.write(s)

    def _q(self, s: str) -> str:
        return self.inst.query(s).strip()

    # ---------- High-level API ----------
    def move_to(self, angle_deg: float) -> None:
        # EPS300 uses "PA" (position absolute) for many axis cards; adjust if yours differs (e.g., "PA 0,<deg>")
        self._cmd(f"PA {float(angle_deg):.3f}")
        # Optionally block until motion complete (MD? returns 0 when done on many Newport controllers)
        try:
            # Poll motion-done (edit if your firmware differs)
            for _ in range(200):
                state = self._q("MD?")
                if state in ("0", "1"):  # 0 or 1 depending on firmware: check manual
                    break
        except Exception:
            pass

    def get_position(self) -> float:
        # Typical query "TP?" returns current position
        resp = self._q("TP?")
        return float(resp)

    def close(self) -> None:
        try:
            self.inst.close()
        except Exception:
            pass
