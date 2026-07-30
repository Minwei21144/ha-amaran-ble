"""Shared fixture color state and Amaran control resolutions."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import AmaranLightConfig
from .telink import TelinkStatus

CCT_STEP_KELVIN = 100
GM_MIN = -10
GM_MAX = 10
HUE_MIN = 0
HUE_MAX = 360
SATURATION_MIN = 0
SATURATION_MAX = 100
INTENSITY_MIN = 0
INTENSITY_MAX = 1000


def clamp_int(value: int | float, minimum: int, maximum: int) -> int:
    """Clamp and round a numeric value to an integer range."""
    return max(minimum, min(maximum, round(value)))


def quantize_kelvin(
    value: int | float,
    minimum: int,
    maximum: int,
) -> int:
    """Clamp color temperature and round it to the nearest 100 K."""
    clamped = clamp_int(value, minimum, maximum)
    quantized = ((clamped + CCT_STEP_KELVIN // 2) // CCT_STEP_KELVIN) * (
        CCT_STEP_KELVIN
    )
    return max(minimum, min(maximum, quantized))


@dataclass
class FixtureColorState:
    """Latest known or successfully requested state for one Mesh fixture."""

    min_kelvin: int
    max_kelvin: int
    supports_hs: bool
    supports_gm: bool
    on: bool | None = None
    is_hs: bool = False
    intensity: int = INTENSITY_MAX
    kelvin: int = 4500
    gm: int = 0
    hue: int = 0
    saturation: int = SATURATION_MAX

    @classmethod
    def from_config(cls, config: AmaranLightConfig) -> FixtureColorState:
        """Build the initial state for a configured fixture."""
        return cls(
            min_kelvin=config.min_kelvin,
            max_kelvin=config.max_kelvin,
            supports_hs=config.supports_hs,
            supports_gm=config.supports_gm,
            kelvin=quantize_kelvin(
                4500,
                config.min_kelvin,
                config.max_kelvin,
            ),
        )

    def snapshot(self) -> FixtureColorState:
        """Return a detached copy suitable for entity callbacks."""
        return replace(self)

    def apply_power(self, on: bool) -> None:
        """Apply a successful power command."""
        self.on = bool(on)

    def apply_intensity(self, intensity: int | float) -> None:
        """Apply a successful intensity command."""
        self.intensity = clamp_int(
            intensity,
            INTENSITY_MIN,
            INTENSITY_MAX,
        )

    def apply_cct(
        self,
        kelvin: int | float,
        intensity: int | float,
        gm: int | float,
    ) -> None:
        """Apply a successful CCT/G/M command."""
        self.is_hs = False
        self.kelvin = quantize_kelvin(
            kelvin,
            self.min_kelvin,
            self.max_kelvin,
        )
        self.gm = (
            clamp_int(gm, GM_MIN, GM_MAX)
            if self.supports_gm
            else 0
        )
        self.apply_intensity(intensity)

    def apply_hsi(
        self,
        hue: int | float,
        saturation: int | float,
        intensity: int | float,
    ) -> None:
        """Apply a successful HSI command."""
        if not self.supports_hs:
            raise ValueError("Fixture does not support HSI")
        self.is_hs = True
        self.hue = clamp_int(hue, HUE_MIN, HUE_MAX)
        self.saturation = clamp_int(
            saturation,
            SATURATION_MIN,
            SATURATION_MAX,
        )
        self.apply_intensity(intensity)

    def apply_status(self, status: TelinkStatus) -> None:
        """Merge a decoded physical fixture status report."""
        self.on = status.on
        self.apply_intensity(status.intensity)
        if status.is_hs and self.supports_hs:
            self.apply_hsi(
                status.hue,
                status.saturation,
                status.intensity,
            )
        elif status.cct_kelvin:
            self.apply_cct(
                status.cct_kelvin,
                status.intensity,
                status.gm,
            )

    def as_dict(self) -> dict[str, bool | int | None | str]:
        """Return a serializable diagnostics snapshot."""
        return {
            "on": self.on,
            "mode": "hs" if self.is_hs else "color_temp",
            "intensity": self.intensity,
            "kelvin": self.kelvin,
            "gm": self.gm,
            "hue": self.hue,
            "saturation": self.saturation,
        }
