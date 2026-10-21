"""Corne battery monitor for Windows: tray icon with both halves' battery levels.

Reads the standard Bluetooth Battery Level characteristic from the paired keyboard.
The left half (central) reports its own level; with
CONFIG_ZMK_SPLIT_BLE_CENTRAL_BATTERY_LEVEL_PROXY it also exposes a second one,
labelled "Peripheral 0", for the right half.

Usage:
  pythonw battery_tray.pyw            tray icon (no console window)
  python  battery_tray.pyw --once     print levels once and exit (for testing)
Requires: pip install bleak pystray pillow   (bleak brings the winrt packages)
"""
import asyncio
import sys
import threading
import uuid

from winrt.windows.devices.bluetooth import BluetoothCacheMode, BluetoothLEDevice
from winrt.windows.devices.enumeration import DeviceInformation

KEYBOARD_NAME = "Corne"
POLL_SECONDS = 300     # how often to read the levels
LOW_PERCENT = 20       # warn below this

BAS_SERVICE = uuid.UUID("0000180f-0000-1000-8000-00805f9b34fb")
BATTERY_LEVEL = uuid.UUID("00002a19-0000-1000-8000-00805f9b34fb")
USER_DESCRIPTION = uuid.UUID("00002901-0000-1000-8000-00805f9b34fb")


def _buffer_bytes(buf):
    return bytes(buf)


async def read_levels():
    """Return {"L": int|None, "R": int|None}, or None if the keyboard isn't paired/connected.

    Retries once: right after pairing or waking, Windows sometimes returns no values.
    """
    levels = await _read_levels_once()
    if levels is not None and levels["L"] is None and levels["R"] is None:
        await asyncio.sleep(2)
        levels = await _read_levels_once()
    return levels


async def _read_levels_once():
    selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
    infos = await DeviceInformation.find_all_async_aqs_filter(selector)
    info = next((i for i in infos if KEYBOARD_NAME.lower() in (i.name or "").lower()), None)
    if info is None:
        return None
    device = await BluetoothLEDevice.from_id_async(info.id)
    if device is None:
        return None
    try:
        levels = {"L": None, "R": None}
        services = await device.get_gatt_services_for_uuid_with_cache_mode_async(
            BAS_SERVICE, BluetoothCacheMode.UNCACHED)
        for service in services.services:
            chars = await service.get_characteristics_for_uuid_with_cache_mode_async(
                BATTERY_LEVEL, BluetoothCacheMode.UNCACHED)
            for ch in chars.characteristics:
                value = await ch.read_value_with_cache_mode_async(BluetoothCacheMode.UNCACHED)
                if value.value is None or value.value.length == 0:
                    continue
                level = _buffer_bytes(value.value)[0]
                label = ""
                descs = await ch.get_descriptors_for_uuid_async(USER_DESCRIPTION)
                for d in descs.descriptors:
                    dv = await d.read_value_async()
                    if dv.value is not None:
                        label = _buffer_bytes(dv.value).decode("utf-8", "ignore")
                levels["R" if label.startswith("Peripheral") else "L"] = level
            service.close()
        return levels
    finally:
        device.close()


def describe(levels):
    if levels is None:
        return f"{KEYBOARD_NAME}: not connected"
    fmt = lambda v: "?" if v is None else f"{v}%"
    return f"{KEYBOARD_NAME}  L {fmt(levels['L'])} · R {fmt(levels['R'])}"


def make_icon(levels):
    """Two vertical bars (left/right half), green/orange/red by level."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i, key in enumerate(("L", "R")):
        x0 = 6 + i * 30
        draw.rectangle([x0, 4, x0 + 22, 60], outline=(200, 200, 200, 255), width=3)
        v = None if levels is None else levels[key]
        if v is None:
            draw.line([x0 + 4, 56, x0 + 18, 8], fill=(150, 150, 150, 255), width=3)
            continue
        color = (60, 200, 90, 255) if v > 50 else (240, 160, 40, 255) if v > LOW_PERCENT else (230, 50, 50, 255)
        top = 57 - int(50 * v / 100)
        draw.rectangle([x0 + 4, top, x0 + 18, 57], fill=color)
    return img


def run_tray():
    import pystray

    state = {"levels": None, "warned": set()}
    wake = threading.Event()
    stop = threading.Event()

    def update(icon):
        try:
            levels = asyncio.run(read_levels())
        except Exception as e:  # keep the tray alive on Bluetooth hiccups
            levels = None
            icon.title = f"{KEYBOARD_NAME}: error ({e.__class__.__name__})"
        else:
            icon.title = describe(levels)
        state["levels"] = levels
        icon.icon = make_icon(levels)
        for key, name in (("L", "Left"), ("R", "Right")):
            v = None if levels is None else levels[key]
            if v is not None and v <= LOW_PERCENT and key not in state["warned"]:
                icon.notify(f"{name} half at {v}% - time to charge.", f"{KEYBOARD_NAME} battery low")
                state["warned"].add(key)
            elif v is not None and v > LOW_PERCENT + 5:
                state["warned"].discard(key)

    def loop(icon):
        icon.visible = True
        while not stop.is_set():
            update(icon)
            wake.wait(POLL_SECONDS)
            wake.clear()

    def on_quit(icon, _):
        stop.set()
        wake.set()
        icon.stop()

    icon = pystray.Icon(
        "corne_battery", make_icon(None), f"{KEYBOARD_NAME}: checking...",
        menu=pystray.Menu(
            pystray.MenuItem(lambda _: describe(state["levels"]), None, enabled=False),
            pystray.MenuItem("Refresh now", lambda *_: wake.set()),
            pystray.MenuItem("Quit", on_quit),
        ))
    icon.run(setup=loop)


if __name__ == "__main__":
    if "--once" in sys.argv:
        print(describe(asyncio.run(read_levels())))
    else:
        run_tray()
