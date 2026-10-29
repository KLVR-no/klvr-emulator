"""Firmware glass via tools/ui-simulator (same path as display CI).

The emulator writes a .state from the live twin status and runs the host
renderer. Pixels match `make display-test`; this module does not invent UI.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_lock = threading.Lock()
_cache_png: bytes | None = None
_cache_at = 0.0
_CACHE_S = 0.75


def _firmware_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("FIRMWARE_ROOT")
    if env:
        roots.append(Path(env))
    roots.append(_REPO.parent / "klvr-wt-twin-fw")
    roots.append(_REPO / "firmware")
    roots.append(_REPO.parent / "klvr-chargerpro-firmware")
    return roots


def render_bin() -> Path | None:
    for root in _firmware_roots():
        bin_path = root / "build" / "ui-simulator" / "render"
        if bin_path.is_file():
            return bin_path
    return None


def _slot_to_state(slot: str, err: str) -> str:
    if slot == "done":
        return "fully_charged"
    if slot == "charging":
        return "charging"
    if slot == "cold":
        return "temp_cold"
    if slot == "warm":
        return "temp_warm"
    if slot == "error":
        return {
            "overtemp": "temp_err",
            "undertemp": "temp_cold",
            "overcurrent": "overcurrent",
            "faulty": "faulty",
            "detect_err": "detect_err",
        }.get(err, "faulty")
    return "charging"


def _mask_prefix(mask) -> int:
    """ui-simulator wants CIDR bits 0..32; the API often sends dotted form."""
    if mask is None:
        return 24
    s = str(mask).strip()
    if s.isdigit():
        return max(0, min(32, int(s)))
    parts = s.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        n = (int(parts[0]) << 24) | (int(parts[1]) << 16) | (int(parts[2]) << 8) | int(parts[3])
        return bin(n & 0xFFFFFFFF).count("1")
    return 24


def status_to_state_text(
    status: dict,
    name: str = "Klvr",
    version: str = "1.9.1",
    *,
    screen: str = "main",
    clicks: int = 0,
    network: dict | None = None,
) -> str:
    """Build a ui-simulator .state from live twin status + knob facts."""
    lines = [
        "time 10000",
        f"screen {screen}",
        f"name {name}",
        f"version {version}",
    ]
    if clicks:
        lines.append(f"clicks {max(0, min(100, int(clicks)))}")
    if screen.startswith("network") and network:
        typ = network.get("type") or "dhcp"
        ip = network.get("ipAddress") or "0.0.0.0"
        gw = network.get("gatewayAddress") or "0.0.0.0"
        mask = _mask_prefix(network.get("mask"))
        mac = network.get("macAddress") or "02:4b:4c:56:52:01"
        lines.append(f"network {typ} ip={ip} gw={gw} mask={mask} mac={mac}")
    for b in status.get("batteries") or []:
        if b.get("slotState") == "empty":
            continue
        det = b.get("batteryDetected") or ""
        kind = "aa" if "AA" in det and "AAA" not in det else "aaa" if "AAA" in det else "aa"
        st = _slot_to_state(b.get("slotState") or "charging", b.get("errorMsg") or "")
        temp = b.get("batteryBayTempC")
        if temp is None:
            temp = 24
        eta_min = int((b.get("timeRemainingSeconds") or 0) / 60)
        lines.append(f"bay {int(b['index'])} {kind} {st} eta={eta_min} temp={int(temp)}")
    return "\n".join(lines) + "\n"


def render_png(
    status: dict,
    name: str = "Klvr",
    version: str = "1.9.1",
    *,
    screen: str = "main",
    clicks: int = 0,
    network: dict | None = None,
) -> bytes | None:
    """Return a 480×128 PNG from the firmware host renderer, or None."""
    global _cache_png, _cache_at
    now = time.monotonic()
    with _lock:
        if _cache_png is not None and (now - _cache_at) < _CACHE_S:
            return _cache_png
    bin_path = render_bin()
    if bin_path is None:
        return None
    text = status_to_state_text(
        status,
        name=name,
        version=version,
        screen=screen,
        clicks=clicks,
        network=network,
    )
    def _run(state_text: str) -> bytes | None:
        with tempfile.TemporaryDirectory(prefix="twin-glass-") as tmp:
            state = Path(tmp) / "live.state"
            out = Path(tmp) / "glass.png"
            state.write_text(state_text)
            try:
                proc = subprocess.run(
                    [str(bin_path), str(state), str(out)],
                    check=True,
                    capture_output=True,
                    timeout=20,
                )
                del proc
            except subprocess.CalledProcessError as exc:
                err = (exc.stderr or b"").decode("utf-8", "replace")[:400]
                print(f"twin glass render failed ({exc.returncode}): {err}", flush=True)
                return None
            except (subprocess.SubprocessError, OSError) as exc:
                print(f"twin glass render error: {exc}", flush=True)
                return None
            if not out.is_file():
                return None
            return out.read_bytes()

    png = _run(text)
    # Network screen needs a valid mask; if it fails, still show MAIN glass.
    if png is None and screen != "main":
        png = _run(
            status_to_state_text(
                status, name=name, version=version, screen="main", clicks=clicks
            )
        )
    if png is None:
        return None
    with _lock:
        _cache_png = png
        _cache_at = time.monotonic()
    return png


def invalidate() -> None:
    global _cache_png, _cache_at
    with _lock:
        _cache_png = None
        _cache_at = 0.0
