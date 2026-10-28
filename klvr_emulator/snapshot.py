"""Firmware charger-twin snapshot via libcharger_twin.

The emulator supplies hardware facts (seat / pull / time). Every slot state,
total, ETA, overlay, and LED colour comes from the pinned firmware library.
This module does not invent charger decisions.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PIN_FILE = _REPO / "firmware.pin"


def firmware_pin() -> str:
    return _PIN_FILE.read_text().strip()


def _lib_candidates() -> list[Path]:
    env = os.environ.get("CHARGER_TWIN_LIB")
    out: list[Path] = []
    if env:
        out.append(Path(env))
    root = os.environ.get("FIRMWARE_ROOT")
    roots = []
    if root:
        roots.append(Path(root))
    roots.append(_REPO / "firmware")
    # Local twin worktree next to this repo.
    roots.append(_REPO.parent / "klvr-wt-twin-fw")
    roots.append(_REPO.parent / "klvr-chargerpro-firmware")
    for r in roots:
        out.append(r / "build" / "charger-twin" / "libcharger_twin.dylib")
        out.append(r / "build" / "charger-twin" / "libcharger_twin.so")
    return out


class FirmwareSnapshot:
    def __init__(self) -> None:
        self._lib = None
        self._started_ms = int(time.time() * 1000)
        # Emulator-owned hardware facts (SoC / remaining advance on the clock).
        self._readings: dict[int, dict] = {}
        self._last_progress_ms = 0
        self._identify = False
        self._load()
        if self._lib is not None:
            self._lib.charger_twin_reset()

    def _load(self) -> None:
        for path in _lib_candidates():
            if not path.is_file():
                continue
            lib = ctypes.CDLL(str(path))
            lib.charger_twin_reset.restype = None
            lib.charger_twin_set_time.argtypes = [ctypes.c_int64, ctypes.c_int]
            lib.charger_twin_set_time.restype = None
            lib.charger_twin_seat_bay.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int32,
                ctypes.c_uint32,
            ]
            lib.charger_twin_seat_bay.restype = None
            lib.charger_twin_pull_bay.argtypes = [ctypes.c_int]
            lib.charger_twin_pull_bay.restype = None
            lib.charger_twin_tick.restype = ctypes.c_void_p
            lib.charger_twin_status_json.restype = ctypes.c_char_p
            lib.charger_twin_totals_json.restype = ctypes.c_char_p
            self._lib = lib
            self._lib_path = path
            return
        self._lib = None
        self._lib_path = None

    @property
    def available(self) -> bool:
        return self._lib is not None

    @property
    def lib_path(self) -> str | None:
        return str(self._lib_path) if self._lib_path else None

    def _now_ms(self) -> int:
        return int(time.time() * 1000) - self._started_ms

    def set_identify(self, on: bool) -> None:
        self._identify = bool(on)

    def _progress_readings(self) -> None:
        """Advance SoC / remaining as hardware facts. Firmware decides states."""
        now = self._now_ms()
        if self._last_progress_ms <= 0:
            self._last_progress_ms = now
            return
        dt_ms = now - self._last_progress_ms
        if dt_ms < 200:
            return
        self._last_progress_ms = now
        # Rough wall-clock charge: ~1 % per 40 s → full from empty in ~67 min.
        dt_s = dt_ms / 1000.0
        for idx, r in list(self._readings.items()):
            if r.get("soc", 0) >= 100:
                r["soc"] = 100
                r["remaining_s"] = 0
                continue
            add = dt_s / 40.0
            r["soc"] = min(100, int(r["soc"] + add))
            rem = int(r.get("remaining_s") or 0)
            if rem > 0:
                r["remaining_s"] = max(0, rem - int(dt_s))
            if r["soc"] >= 100:
                r["soc"] = 100
                r["remaining_s"] = 0
            self._lib.charger_twin_seat_bay(
                idx, r["type"], r["soc"], r["temp_c"], int(r["remaining_s"])
            )

    def tick(self) -> dict:
        if self._lib is None:
            raise RuntimeError(
                "libcharger_twin not found. Build tools/charger-twin from the "
                f"pinned firmware commit ({firmware_pin()}) and set CHARGER_TWIN_LIB "
                "or FIRMWARE_ROOT."
            )
        self._progress_readings()
        self._lib.charger_twin_set_time(self._now_ms(), 1 if self._identify else 0)
        self._lib.charger_twin_tick()
        raw = self._lib.charger_twin_status_json()
        if not raw:
            raise RuntimeError("charger_twin_status_json returned empty")
        return json.loads(raw.decode("utf-8"))

    def seat(self, index: int, type_code: int, soc: int, temp_c: int = 24, remaining_s: int = 0) -> dict:
        self._readings[index] = {
            "type": type_code,
            "soc": soc,
            "temp_c": temp_c,
            "remaining_s": remaining_s,
        }
        self._lib.charger_twin_seat_bay(index, type_code, soc, temp_c, remaining_s)
        return self.tick()

    def pull(self, index: int) -> dict:
        self._readings.pop(index, None)
        self._lib.charger_twin_pull_bay(index)
        return self.tick()

    def clear_all(self) -> dict:
        self._readings.clear()
        for i in range(48):
            self._lib.charger_twin_pull_bay(i)
        return self.tick()


# Type codes match charger_thermal_bay / BATTERY_TYPE_*.
TYPE_AA = 2
TYPE_AAA = 1


def type_code(name: str) -> int:
    if name == "KLVR-AA":
        return TYPE_AA
    if name == "KLVR-AAA":
        return TYPE_AAA
    raise ValueError(name)
