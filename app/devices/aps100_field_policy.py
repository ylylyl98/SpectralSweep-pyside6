"""Operation limits for the attoDRY1000 9 T magnet.

Continuous driven operation is limited to 8 T. A guarded move may temporarily
exceed that limit to finish Persistent, or to return from a persistent high field.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class APS100FieldLimits:
    maximum_field_t: float = 9.0
    driven_max_field_t: float = 8.0
    persistent_max_field_t: float = 9.0
    mcd_max_field_t: float = 8.0

    @classmethod
    def from_config(cls, config):
        return cls(config.maximum_field_t, config.safe_control_max_field_t,
                   config.persistent_control_max_field_t, config.mcd_max_field_t)

    @staticmethod
    def _limit(*values):
        values = tuple(float(value) for value in values)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("APS100 field limits must be finite and positive")
        return min(values)

    def manual_limit(self, final_mode):
        mode = str(final_mode).strip().lower()
        if mode == "driven":
            return self._limit(8.0, self.maximum_field_t, self.driven_max_field_t)
        if mode == "persistent":
            return self._limit(9.0, self.maximum_field_t, self.persistent_max_field_t)
        raise ValueError("Final magnet mode must be driven or persistent")

    @property
    def mcd_limit(self):
        return self._limit(self.manual_limit("driven"), self.mcd_max_field_t)

    def validate_target(self, target_t, final_mode):
        limit = self.manual_limit(final_mode)
        target = float(target_t)
        if isinstance(target_t, bool) or not math.isfinite(target):
            raise ValueError("Magnetic field must be finite")
        if abs(target) > limit:
            guidance = (
                " Use a controlled move ending Persistent for higher fields, "
                "or return within the driven limit before enabling continuous operation."
                if str(final_mode).strip().lower() == "driven"
                else " The persistent target must stay within the magnet's configured ceiling."
            )
            raise ValueError(
                f"{str(final_mode).strip().capitalize()} target exceeds ±{limit:g} T.{guidance}"
            )
        return target
