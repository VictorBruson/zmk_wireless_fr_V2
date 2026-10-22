"""Corne layer display for Windows: a small always-on-top window that shows the active layer.

How it knows the layer: the firmware holds an unused F-key while a layer is held
(F13 Nav, F14 Media, F15 Num, F16 Sym, F17 Fun, F18 Mouse); a lock taps F21 then that
layer's key, F19 = to Base, F20 = to Gaming. See "Layer signals" in config/corne.keymap.
This program catches those keys with a keyboard hook and swallows them, so no other
app sees them. It reads the layout from config/corne.keymap, so it follows keymap edits
(restart it after editing).

Ctrl+Alt+K shows or hides the window. Drag it to move it; right-click for options.
Run with:  pythonw layer_overlay.pyw
"""
import ctypes
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

KEYMAP = Path(__file__).resolve().parent.parent / "config" / "corne.keymap"
SETTINGS = Path(os.environ.get("APPDATA", Path.home())) / "corne_layer_overlay.json"
HOTKEY_MODS = 0x0001 | 0x0002   # MOD_ALT | MOD_CONTROL
HOTKEY_VK = ord("K")
UNIT = 40                       # key size in pixels

VK_F13 = 0x7C
# Held layers: F13..F18. A lock taps F21 (prefix) then the layer's held key, and switches
# to the "locked copy" layer (NAV_L ...), see corne.keymap. F19 = to Base, F20 = to Gaming.
HELD_SIGNALS = {VK_F13 + 0: "NAV", VK_F13 + 1: "MEDIA", VK_F13 + 2: "NUM", VK_F13 + 3: "SYM",
                VK_F13 + 4: "FUN", VK_F13 + 5: "MOUSE"}
LOCK_SIGNALS = {VK_F13 + 6: "BASE", VK_F13 + 7: "GAMING"}
LOCK_PREFIX = VK_F13 + 8
# Dead keys: F22-F24 = the accent layer is active for the next key only.
STICKY_SIGNALS = {VK_F13 + 9: "ACC_GRAVE", VK_F13 + 10: "ACC_CIRC", VK_F13 + 11: "ACC_TREMA"}
STICKY_SECONDS = 5.0  # matches release-after-ms in corne.keymap
ALL_SIGNALS = set(HELD_SIGNALS) | set(LOCK_SIGNALS) | {LOCK_PREFIX} | set(STICKY_SIGNALS)
# Keys that don't use up a dead key (ZMK's ignore-modifiers): Shift, Ctrl, Alt, Win, Caps Lock.
MODIFIER_VKS = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C} | set(range(0xA0, 0xA6))

# French characters typed by the keymap's fr_* macros (Alt codes): (normal, with Shift)
FR_LABELS = {
    "fr_e_acute": ("é", "É"), "fr_c_cedil": ("ç", "Ç"), "fr_guill": ("«", "»"), "fr_oe": ("œ", "Œ"),
    "fr_a_grave": ("à", "À"), "fr_e_grave": ("è", "È"), "fr_u_grave": ("ù", "Ù"),
    "fr_a_circ": ("â", "Â"), "fr_e_circ": ("ê", "Ê"), "fr_i_circ": ("î", "Î"), "fr_o_circ": ("ô", "Ô"),
    "fr_u_circ": ("û", "Û"), "fr_e_trema": ("ë", "Ë"), "fr_i_trema": ("ï", "Ï"), "fr_u_trema": ("ü", "Ü"),
    "fr_y_trema": ("ÿ", "Ÿ"), "fr_trema": ("¨", "¨"),
    "dead_grave": ("`", "`"), "dead_circ_trema": ("^", "¨"),
}

# ---------------------------------------------------------------- keymap parsing

KEY_LABELS = {
    "SQT": "'", "COMMA": ",", "DOT": ".", "FSLH": "/", "SEMI": ";", "EQUAL": "=", "GRAVE": "`",
    "BSLH": "\\", "MINUS": "-", "LBKT": "[", "RBKT": "]", "LBRC": "{", "RBRC": "}", "AMPS": "&",
    "ASTRK": "*", "LPAR": "(", "RPAR": ")", "COLON": ":", "DLLR": "$", "PRCNT": "%", "CARET": "^",
    "PLUS": "+", "TILDE": "~", "EXCL": "!", "AT": "@", "HASH": "#", "PIPE": "|", "UNDER": "_",
    "LT": "<", "GT": ">", "DQT": "\"", "QMARK": "?",
    "SPACE": "Space", "RET": "Enter", "BSPC": "Bksp", "DEL": "Del", "ESC": "Esc", "TAB": "Tab",
    "LEFT": "←", "RIGHT": "→", "UP": "↑", "DOWN": "↓", "PG_UP": "PgUp", "PG_DN": "PgDn",
    "HOME": "Home", "END": "End", "INS": "Insert", "PSCRN": "PrtSc", "SLCK": "ScrLk",
    "PAUSE_BREAK": "Pause", "K_APP": "Menu", "C_PREV": "Prev", "C_NEXT": "Next",
    "C_VOL_UP": "Vol +", "C_VOL_DN": "Vol −", "C_PP": "Play", "C_MUTE": "Mute", "C_STOP": "Stop",
    "LSHFT": "Shift", "RSHFT": "Shift", "LCTRL": "Ctrl", "RCTRL": "Ctrl", "LALT": "Alt",
    "RALT": "AltGr", "LGUI": "Gui", "RGUI": "Gui",
}
MODS = {"LSHFT", "RSHFT", "LCTRL", "RCTRL", "LALT", "RALT", "LGUI", "RGUI"}
NAV_KEYS = {"LEFT", "RIGHT", "UP", "DOWN", "PG_UP", "PG_DN", "HOME", "END", "INS"}


def key_label(code):
    if code in KEY_LABELS:
        return KEY_LABELS[code]
    if re.fullmatch(r"N\d", code):
        return code[1]
    return code.title() if len(code) > 3 and not re.fullmatch(r"F\d+", code) else code


def parse_keymap(path):
    """Return (layer_defines {NAME: index}, layers [ {name, keys:[(kind, tap, hold)]} ])."""
    # locale.h holds the K_* character keys; read it like the #include in the keymap does.
    locale = path.with_name("locale.h")
    text = (locale.read_text(encoding="utf-8") if locale.exists() else "") + "\n" + path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    # Use the US variant of #if defined(LAYOUT_CA_FR) blocks: both firmware variants type the
    # same characters, and the US variant names them with plain keycodes.
    lines, stack = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#if"):
            variant = "LAYOUT_CA_FR" in s
            stack.append([not variant, variant])  # [active, is the variant switch]
        elif s.startswith("#else") and stack:
            if stack[-1][1]:
                stack[-1][0] = not stack[-1][0]
        elif s.startswith("#endif") and stack:
            stack.pop()
        elif all(active for active, _ in stack):
            lines.append(line)
    text = "\n".join(lines)
    text = text.replace("\\\n", " ")  # join #define continuation lines
    defines = {m[1]: int(m[2]) for m in re.finditer(r"^#define\s+(\w+)\s+(\d+)\s*$", text, flags=re.M)}
    # Object-like macros holding bindings, e.g. #define NAV_KEYS &trans &kp A ...
    binding_macros = {m[1]: m[2] for m in re.finditer(r"^#define\s+(\w+)\s+(&.*)$", text, flags=re.M)}
    names_by_index = {}
    keymap = text[text.index("compatible = \"zmk,keymap\""):]
    layers = []
    for m in re.finditer(r"display-name\s*=\s*\"([^\"]+)\";\s*bindings\s*=\s*<(.*?)>;", keymap, flags=re.S):
        raw = m[2]
        for _ in range(5):  # macros can contain macros (NUM_KEYS -> K_N7 -> &kp N7)
            new = re.sub(r"\b[A-Z_][A-Z0-9_]*\b", lambda w: binding_macros.get(w[0], w[0]), raw)
            if new == raw:
                break
            raw = new
        names_by_index[len(layers)] = m[1]
        layers.append({"name": m[1], "raw": raw})

    def layer_name(token):
        token = token.upper()
        idx = defines.get(token)
        return names_by_index.get(idx, token.title()) if idx is not None else token.title()

    for layer in layers:
        keys = []
        for part in layer.pop("raw").split("&")[1:]:
            name, *params = part.split()
            # 4th field: the ZMK keycode that types a character (used for layout/Shift labels)
            code = params[0] if name == "kp" else params[1] if name in ("mt", "lt") or name.startswith("lt_") else None
            if name in FR_LABELS:
                code = ("fr",) + FR_LABELS[name]  # fixed characters, Shift picks the capital
            elif code not in CHAR_KEYS:
                code = None
            keys.append(convert(name, params, layer_name) + (code,))
        layer["keys"] = keys
    return defines, layers


def convert(name, p, layer_name):
    if name == "kp":
        code = p[0]
        kind = "m" if code in MODS else "n" if code in NAV_KEYS else "f" if code.startswith("C_") else "a"
        return (kind, key_label(code), None)
    if name == "mt":
        return ("a", key_label(p[1]), key_label(p[0]))
    if name == "lt":
        return ("a", key_label(p[1]), "▸ " + layer_name(p[0]))
    if name.startswith("lt_"):
        return ("a", key_label(p[1]), "▸ " + layer_name(name[3:]))
    if name in ("mo",) or name.startswith("sig_"):
        return ("l", layer_name(p[0] if name == "mo" else name[4:]), "hold")
    if name == "to" or name.startswith("to_"):
        target = p[0] if name == "to" else name[3:]
        label = layer_name(target)
        return ("l", "To Base" if target.upper() == "BASE" else "Lock " + label, None)
    if name == "trans":
        return ("t", "", None)
    if name == "none":
        return ("x", "", None)
    if name == "bootloader":
        return ("d", "Boot", None)
    if name == "td_gaming":
        return ("s", "Gaming ×2", None)
    if name == "caps_word":
        return ("s", "Caps Word", None)
    if name == "studio_unlock":
        return ("s", "Studio unlock", None)
    if name == "out":
        return ("f", {"OUT_USB": "USB", "OUT_BLE": "Bluetooth", "OUT_TOG": "USB/BT"}.get(p[0], p[0]), None)
    if name in ("mmv", "msc"):
        arrow = {"LEFT": "←", "RIGHT": "→", "UP": "↑", "DOWN": "↓"}.get(p[0].split("_")[-1], p[0])
        return ("n", ("Move " if name == "mmv" else "Scroll ") + arrow, None)
    if name == "mkp":
        return ("a", {"LCLK": "Left click", "RCLK": "Right click", "MCLK": "Middle click"}.get(p[0], p[0]), None)
    if name.startswith("dead_"):
        return ("s", FR_LABELS.get(name, ("?",))[0], "accent")
    if name in FR_LABELS:
        return ("a", FR_LABELS[name][0], None)
    if name == "bt":
        if p[0] == "BT_SEL":
            return ("f", "BT " + p[1], None)
        return ("f", p[0].replace("BT_", "BT ").title(), None)
    return ("s", name, None)


# ---------------------------------------------------------------- characters per Windows layout
# ZMK keycode -> (PC scancode, needs Shift). Windows turns the scancode into a character
# using the active keyboard layout, so labels follow US / Canadian French / etc.
_SC = {**{c: s for c, s in zip("QWERTYUIOP", range(0x10, 0x1A))},
       **{c: s for c, s in zip("ASDFGHJKL", range(0x1E, 0x27))},
       **{c: s for c, s in zip("ZXCVBNM", range(0x2C, 0x33))},
       **{f"N{d}": s for d, s in zip("1234567890", range(0x02, 0x0C))},
       "MINUS": 0x0C, "EQUAL": 0x0D, "LBKT": 0x1A, "RBKT": 0x1B, "BSLH": 0x2B, "SEMI": 0x27,
       "SQT": 0x28, "GRAVE": 0x29, "COMMA": 0x33, "DOT": 0x34, "FSLH": 0x35}
_SHIFTED = {"EXCL": "N1", "AT": "N2", "HASH": "N3", "DLLR": "N4", "PRCNT": "N5", "CARET": "N6",
            "AMPS": "N7", "ASTRK": "N8", "LPAR": "N9", "RPAR": "N0", "UNDER": "MINUS", "PLUS": "EQUAL",
            "LBRC": "LBKT", "RBRC": "RBKT", "PIPE": "BSLH", "COLON": "SEMI", "TILDE": "GRAVE",
            "DQT": "SQT", "LT": "COMMA", "GT": "DOT", "QMARK": "FSLH"}
CHAR_KEYS = {**{k: (s, False) for k, s in _SC.items()}, **{k: (_SC[b], True) for k, b in _SHIFTED.items()}}


class KeyChars:
    """Ask Windows which character a key gives in the foreground window's layout."""

    def __init__(self):
        self.u32 = ctypes.WinDLL("user32")
        self.u32.GetKeyboardLayout.restype = ctypes.c_void_p
        self.u32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        self.u32.GetForegroundWindow.restype = wintypes.HWND
        self.u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
        self.u32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
        self.u32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_char_p, wintypes.LPWSTR,
                                         ctypes.c_int, wintypes.UINT, ctypes.c_void_p]
        self.u32.LoadKeyboardLayoutW.restype = ctypes.c_void_p
        self.us = self.u32.LoadKeyboardLayoutW("00000409", 0x80)  # already installed; not activated
        self.hkl = self.current_layout()
        self.cache = {}

    def cafr(self):
        """Windows is on Canadian French: assume the Canadian French firmware is flashed."""
        return ((self.hkl or 0) >> 16) & 0xFFFF == 0x1009

    def label_layout(self):
        # The Canadian French firmware types the same characters as the US one on the US layout,
        # so its labels come from the US table.
        return self.us if self.cafr() else self.hkl

    def current_layout(self):
        hwnd = self.u32.GetForegroundWindow()
        tid = self.u32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
        return self.u32.GetKeyboardLayout(tid) or self.u32.GetKeyboardLayout(0)

    def refresh(self):
        """Return True if the layout changed."""
        hkl = self.current_layout()
        if hkl and hkl != self.hkl:
            self.hkl = hkl
            return True
        return False

    def layout_name(self):
        # High word of the layout handle = the keyboard layout (low word = language).
        layout = ((self.hkl or 0) >> 16) & 0xFFFF
        return {0x0409: "US", 0x1009: "Canadian French (needs the Canadian French firmware)",
                0x040C: "French AZERTY (no matching firmware)",
                0x0C0C: "Canadian French legacy (no matching firmware)"}.get(layout, f"layout {layout:04X}")

    def has_altgr(self):
        """US-style layouts have no AltGr characters; there Right Alt is plain Alt."""
        if self.cafr():
            return False  # AltGr symbols are already on Sym; labels follow the US table here
        key = ("altgr?", self.hkl)
        if key not in self.cache:
            self.cache[key] = any(self.char(k, False, True) for k in CHAR_KEYS if k in _SC)
        return self.cache[key]

    def char(self, code, shift, altgr):
        scan, implicit_shift = CHAR_KEYS[code]
        shift = shift or implicit_shift
        hkl = self.label_layout()
        key = (hkl, scan, shift, altgr)
        if key not in self.cache:
            vk = self.u32.MapVirtualKeyExW(scan, 1, hkl)  # MAPVK_VSC_TO_VK
            state = (ctypes.c_ubyte * 256)()
            if shift:
                state[0x10] = state[0xA0] = 0x80
            if altgr:
                state[0x11] = state[0xA2] = state[0x12] = state[0xA5] = 0x80
            buf = ctypes.create_unicode_buffer(8)
            # flag 0x4: don't touch the keyboard state, so dead keys in other apps keep working
            n = self.u32.ToUnicodeEx(vk, scan, ctypes.cast(state, ctypes.c_char_p), buf, 8, 0x4, hkl)
            text = buf.value[:abs(n)] if n else ""
            self.cache[key] = text if text.isprintable() else ""
        return self.cache[key]


# ---------------------------------------------------------------- layer state (mirrors ZMK)

class LayerState:
    """ZMK rules: highest active layer wins per key, &to clears all others, releasing &mo turns its layer off."""

    def __init__(self, defines):
        self.defines = defines
        self.active = set()
        self.sticky = None        # one-shot accent layer index
        self.sticky_until = 0.0

    def set_sticky(self, name):
        self.sticky = self.defines.get(name)
        self.sticky_until = time.monotonic() + STICKY_SECONDS

    def clear_sticky(self):
        changed = self.sticky is not None
        self.sticky = None
        return changed

    def held(self, name, down):
        idx = self.defines.get(name)
        if idx is None:
            return
        (self.active.add if down else self.active.discard)(idx)

    def lock(self, name):
        idx = self.defines.get(name, 0)
        self.active = {idx} if idx else set()

    def stack(self):
        extra = {self.sticky} if self.sticky is not None else set()
        return sorted(self.active | extra | {0}, reverse=True)


# ---------------------------------------------------------------- Windows hook + hotkey

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

WH_KEYBOARD_LL, WM_HOTKEY = 13, 0x0312
VK_LSHIFT, VK_RSHIFT, VK_RMENU = 0xA0, 0xA1, 0xA5
MOD_KEYS = {VK_LSHIFT, VK_RSHIFT, VK_RMENU}
KEYDOWN = {0x0100, 0x0104}
KEYUP = {0x0101, 0x0105}


def input_thread(events):
    def proc(n_code, w_param, l_param):
        if n_code == 0:
            vk = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
            if vk in MOD_KEYS:  # Shift / AltGr: watch, never swallow
                events.put(("mod", (vk, w_param in KEYDOWN)))
            elif vk not in ALL_SIGNALS:
                if w_param in KEYDOWN and vk not in MODIFIER_VKS:
                    events.put(("key", vk))  # any other key ends a dead-key accent layer
            else:
                if w_param in KEYDOWN:
                    events.put(("down", vk))
                elif w_param in KEYUP:
                    events.put(("up", vk))
                return 1  # swallow: no other app sees the signal keys
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    callback = HOOKPROC(proc)
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, callback, kernel32.GetModuleHandleW(None), 0)
    if not hook:
        events.put(("error", "keyboard hook failed"))
        return
    if not user32.RegisterHotKey(None, 1, HOTKEY_MODS | 0x4000, HOTKEY_VK):  # 0x4000 = MOD_NOREPEAT
        events.put(("error", "Ctrl+Alt+K is used by another app"))
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            events.put(("toggle", None))
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# ---------------------------------------------------------------- window

COLORS = {
    "bg": "#16191f", "cap": "#262b34", "edge": "#3b424e", "ink": "#e7eaf0", "muted": "#7f8899",
    "a": "#e7eaf0", "m": "#b899f2", "l": "#84a1ff", "n": "#4ec8af", "f": "#f0a650",
    "s": "#dcbc4e", "d": "#ff6e6e", "held": "#84a1ff",
}
STAGGER = [0.375, 0.375, 0.125, 0, 0.125, 0.25]
HALF_GAP = 7.7
HEADER = 30


def place(pos):
    if pos >= 36:
        left = [(3.15, 3.55), (4.2, 3.6), (5.25, 3.75)]
        t = pos - 36
        if t < 3:
            return left[t]
        x, y = left[5 - t]
        return HALF_GAP + (5 - x), y
    row, col = divmod(pos, 12)
    if col < 6:
        return col, row + STAGGER[col]
    c = col - 6
    return HALF_GAP + c, row + STAGGER[5 - c]


class Overlay:
    def __init__(self):
        self.defines, self.layers = parse_keymap(KEYMAP)
        self.state = LayerState(self.defines)
        self.events = queue.Queue()
        self.error = None
        self.lock_pending = None
        self.shift_keys = set()
        self.altgr = False
        self.ticks = 0
        self.chars = KeyChars()

        self.root = tk.Tk()
        self.root.title("Corne layers")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        w, h = int(13.7 * UNIT) + 16, int(4.75 * UNIT) + HEADER + 12
        self.canvas = tk.Canvas(self.root, width=w, height=h, bg=COLORS["bg"], highlightthickness=0)
        self.canvas.pack()
        settings = self.load_settings()
        x, y = settings.get("x"), settings.get("y")
        if x is None:
            x = self.root.winfo_screenwidth() - w - 24
            y = self.root.winfo_screenheight() - h - 72
        self.root.geometry(f"+{x}+{y}")
        self.visible = settings.get("visible", True)
        if not self.visible:
            self.root.withdraw()

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Hide  (Ctrl+Alt+K)", command=self.toggle)
        self.menu.add_command(label="Reset to Base", command=self.reset)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.root.destroy)
        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag_move)
        self.canvas.bind("<ButtonRelease-1>", lambda e: self.save_settings())
        self.canvas.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        threading.Thread(target=input_thread, args=(self.events,), daemon=True).start()
        self.draw()
        self.root.after(15, self.poll)

    # settings
    def load_settings(self):
        try:
            return json.loads(SETTINGS.read_text())
        except Exception:
            return {}

    def save_settings(self):
        try:
            SETTINGS.write_text(json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                                            "visible": self.visible}))
        except Exception:
            pass

    # interaction
    def drag_start(self, e):
        self._drag = (e.x, e.y)

    def drag_move(self, e):
        dx, dy = self._drag
        self.root.geometry(f"+{self.root.winfo_x() + e.x - dx}+{self.root.winfo_y() + e.y - dy}")

    def toggle(self):
        self.visible = not self.visible
        if self.visible:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.draw()
        else:
            self.root.withdraw()
        self.save_settings()

    def reset(self):
        self.state.lock("BASE")
        self.draw()

    def poll(self):
        changed = False
        try:
            while True:
                kind, vk = self.events.get_nowait()
                if kind == "toggle":
                    self.toggle()
                elif kind == "mod":
                    mod_vk, down = vk
                    before = (bool(self.shift_keys), self.altgr)
                    if mod_vk == VK_RMENU:
                        self.altgr = down
                    else:
                        (self.shift_keys.add if down else self.shift_keys.discard)(mod_vk)
                    changed |= before != (bool(self.shift_keys), self.altgr)
                elif kind == "key":
                    changed |= self.state.clear_sticky()
                elif kind == "error":
                    self.error = vk
                    changed = True
                elif vk in STICKY_SIGNALS:
                    if kind == "down":
                        self.state.set_sticky(STICKY_SIGNALS[vk])
                        changed = True
                elif vk == LOCK_PREFIX:
                    if kind == "down":
                        self.lock_pending = time.monotonic()
                elif vk in HELD_SIGNALS and self.lock_pending and time.monotonic() - self.lock_pending < 0.5:
                    # F21 then a layer key = lock that layer (its locked copy).
                    if kind == "up":
                        self.state.lock(HELD_SIGNALS[vk] + "_L")
                        self.lock_pending = None
                        changed = True
                elif vk in HELD_SIGNALS:
                    self.state.held(HELD_SIGNALS[vk], kind == "down")
                    changed = True
                elif vk in LOCK_SIGNALS and kind == "down":
                    self.state.lock(LOCK_SIGNALS[vk])
                    changed = True
        except queue.Empty:
            pass
        self.ticks += 1
        if self.ticks % 20 == 0 and self.chars.refresh():  # layout switched (e.g. US <-> FR-CA)
            changed = True
        if self.state.sticky is not None and time.monotonic() > self.state.sticky_until:
            changed |= self.state.clear_sticky()  # dead key timed out
        if changed and self.visible:
            self.draw()
        self.root.after(15, self.poll)

    # drawing
    def resolve(self, pos, stack):
        for i, idx in enumerate(stack):
            if idx < len(self.layers):
                key = self.layers[idx]["keys"][pos]
                if key[0] != "t":
                    return key, i > 0
        return ("x", "", None, None), False

    def draw(self):
        c = self.canvas
        c.delete("all")
        stack = self.state.stack()
        top = self.layers[stack[0]]["name"] if stack[0] < len(self.layers) else "?"
        title = top if len(stack) <= 2 else f"{top}  (over {', '.join(self.layers[i]['name'] for i in stack[1:-1])})"
        c.create_text(12, HEADER // 2 + 2, text=title, anchor="w", fill=COLORS["ink"],
                      font=("Segoe UI Semibold", 12))
        shift, altgr = bool(self.shift_keys), self.altgr and self.chars.has_altgr()
        mods = " + ".join(m for m, on in (("⇧ Shift", shift), ("AltGr", altgr)) if on)
        right = self.error or (f"{self.chars.layout_name()} · {mods}" if mods
                               else f"{self.chars.layout_name()} · Ctrl+Alt+K hide · right-click menu")
        c.create_text(int(13.7 * UNIT) + 4, HEADER // 2 + 2, anchor="e",
                      fill=COLORS["s"] if mods else COLORS["muted"],
                      font=("Segoe UI", 9 if mods else 8, "bold" if mods else "normal"), text=right)
        base = self.layers[0]["keys"]
        held_layers = {self.layers[i]["name"] for i in self.state.active if i < len(self.layers)}
        for pos in range(len(base)):
            (kind, tap, hold, code), dim = self.resolve(pos, stack)
            if isinstance(code, tuple):  # French character / dead key: fixed, Shift = capital
                tap = code[2] if shift else code[1]
            elif code:
                ch = self.chars.char(code, shift, altgr)
                # Letters: lowercase normally, capitals with Shift, so Shift is visible at a glance.
                tap = ch or ("" if altgr else tap)  # AltGr with nothing on that key: blank
                if not shift and len(tap) == 1 and tap.isalpha():
                    tap = tap.lower()
            x, y = place(pos)
            x0, y0 = 8 + x * UNIT, HEADER + 4 + y * UNIT
            x1, y1 = x0 + UNIT * 0.93, y0 + UNIT * 0.93
            base_hold = base[pos][2] or ""
            is_held = base_hold.startswith("▸") and base_hold[2:] in held_layers and stack[0] != 0
            if kind == "x":
                self.round_rect(x0, y0, x1, y1, outline=COLORS["edge"], fill="", dash=(3, 3))
                continue
            self.round_rect(x0, y0, x1, y1, outline=COLORS["d"] if kind == "d" else COLORS["edge"],
                            fill=COLORS["held"] if is_held else COLORS["cap"])
            color = COLORS["bg"] if is_held else COLORS["muted"] if dim else COLORS.get(kind, COLORS["ink"])
            size = 11 if len(tap) <= 2 else 8 if len(tap) <= 5 else 7
            cy = (y0 + y1) / 2 - (5 if hold else 0)
            c.create_text((x0 + x1) / 2, cy, text=tap, fill=color, width=UNIT * 0.9, justify="center",
                          font=("Segoe UI", size, "bold" if not dim else "normal"))
            if hold and not dim:
                hcolor = COLORS["bg"] if is_held else COLORS["l"] if hold.startswith("▸") or hold == "hold" else COLORS["m"]
                c.create_text((x0 + x1) / 2, y1 - 8, text=hold, fill=hcolor, font=("Segoe UI", 7, "bold"))
            hint = base[pos][1]
            if stack[0] != 0 and hint and len(hint) <= 2 and not dim:
                c.create_text(x0 + 4, y0 + 7, text=hint, anchor="w", fill=COLORS["muted"], font=("Consolas", 7))

    def round_rect(self, x0, y0, x1, y1, r=6, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.canvas.create_polygon(pts, smooth=True, **kw)


if __name__ == "__main__":
    Overlay().root.mainloop()
