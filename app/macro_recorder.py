"""
Macro recorder using the `keyboard` library for global hotkey capture on Windows.

Features:
- Records key combinations as a single action (modifiers + primary key).
- Optionally records real-time delays between combinations or uses a default delay.
- Emits callbacks on action added and on stop.
- Enforces MAX_COMBOS (combinations count).

Note: The `keyboard` module requires elevated privileges on some systems. This
application targets Windows and expects sufficient permissions.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Dict, Optional

try:
    import keyboard  # type: ignore
except Exception:  # pragma: no cover - environment without keyboard module
    keyboard = None  # type: ignore


DEFAULT_DELAY_MS = 10
MAX_COMBOS = 20

MOD_SHIFT = 0x01
MOD_CTRL = 0x02
MOD_ALT = 0x04
MOD_GUI = 0x08

MOD_NAMES = {
    'shift': MOD_SHIFT,
    'left shift': MOD_SHIFT,
    'right shift': MOD_SHIFT,
    'ctrl': MOD_CTRL,
    'left ctrl': MOD_CTRL,
    'right ctrl': MOD_CTRL,
    'alt': MOD_ALT,
    'left alt': MOD_ALT,
    'right alt': MOD_ALT,
    'windows': MOD_GUI,
    'left windows': MOD_GUI,
    'right windows': MOD_GUI,
    'win': MOD_GUI,
}

SPECIAL_KEYS = {
    'esc': 'ESC',
    'escape': 'ESC',
    'tab': 'TAB',
    'enter': 'ENTER',
    'return': 'ENTER',
    'space': 'SPACE',
    'backspace': 'BACKSPACE',
    'delete': 'DELETE',
    'home': 'HOME',
    'end': 'END',
    'page up': 'PAGEUP',
    'page down': 'PAGEDOWN',
    'left': 'LEFT',
    'right': 'RIGHT',
    'up': 'UP',
    'down': 'DOWN',
}

for i in range(1, 13):
    SPECIAL_KEYS[f'f{i}'] = f'F{i}'


class MacroRecorder:
    """Global keyboard macro recorder.

    Usage:
      rec = MacroRecorder(use_real_delays: bool)
      rec.on_action = lambda action: ...
      rec.start()
      rec.stop()

    - Actions emitted as dicts:
      - {'type': 'key', 'mods': int_modmask, 'key': 'E'|'F1'|'TAB'|..., 'ts': float_seconds}
      - {'type': 'delay', 'ms': int_ms}
    """

    def __init__(self, use_real_delays: bool = False) -> None:
        self.use_real_delays = use_real_delays
        self.on_action: Optional[Callable[[Dict], None]] = None
        self.on_stop: Optional[Callable[[], None]] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._combos_count = 0
        self._last_action_time = time.monotonic()
        self._pressed_mods: int = 0
        self._pressed_keys: set[str] = set()
        # Build a scan-code-to-token map to be layout-agnostic for letters/digits
        self._scancode_to_token: Dict[int, str] = {}
        try:
            if keyboard is not None:
                for ch in 'abcdefghijklmnopqrstuvwxyz':
                    for sc in keyboard.key_to_scan_codes(ch):
                        self._scancode_to_token[sc] = ch.upper()
                for d in '0123456789':
                    for sc in keyboard.key_to_scan_codes(d):
                        self._scancode_to_token[sc] = d
        except Exception:
            pass

    def start(self) -> None:
        """Start recording in a background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._combos_count = 0
        self._last_action_time = time.monotonic()
        self._pressed_mods = 0
        self._pressed_keys.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop recording."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        if self.on_stop:
            self.on_stop()

    # ----------------- Internal -----------------
    def _run(self) -> None:
        if keyboard is None:
            # No keyboard module available
            if self.on_action:
                self.on_action({'type': 'delay', 'ms': DEFAULT_DELAY_MS})
            if self.on_stop:
                self.on_stop()
            return
        # Set hooks
        keyboard.hook(self._handle_event, suppress=False)
        try:
            while not self._stop_event.is_set():
                time.sleep(0.02)
        finally:
            keyboard.unhook_all()

    def _handle_event(self, event) -> None:
        if self._stop_event.is_set():
            return
        # Only consider key down events for combos
        if event.event_type not in ('down', 'up'):
            return
        name = (event.name or '').lower()
        if not name:
            return
        is_down = event.event_type == 'down'

        # Track modifiers state continually
        if name in MOD_NAMES:
            bit = MOD_NAMES[name]
            if is_down:
                self._pressed_mods |= bit
            else:
                self._pressed_mods &= ~bit
            return

        # For non-modifier keys, handle on 'down' only to avoid duplicates
        if is_down:
            # Generate action only when this key wasn't already pressed
            if name in self._pressed_keys:
                return
            self._pressed_keys.add(name)

            # Map to canonical key token
            key_token = self._name_to_token(name)
            # If token not valid (e.g., non-Latin letters), try scan code mapping to A..Z/0..9
            if key_token is None or (len(key_token) == 1 and not (key_token.isdigit() or ('A' <= key_token <= 'Z'))):
                try:
                    sc = getattr(event, 'scan_code', None)
                    if sc is not None:
                        mapped = self._scancode_to_token.get(sc)
                        if mapped:
                            key_token = mapped
                except Exception:
                    pass
            if key_token is None:
                return
            # Special handling: Shift + digit should still record the digit key token
            # keyboard library gives ')' for Shift+9 etc.; normalize to underlying digit
            if key_token in (
                ')','!','@','#','$','%','^','&','*','(',
            ):
                # Map shifted symbols back to their digit counterparts
                shifted_map = {
                    ')': '9',
                    '!': '1',
                    '@': '2',
                    '#': '3',
                    '$': '4',
                    '%': '5',
                    '^': '6',
                    '&': '7',
                    '*': '8',
                    '(': '0',
                }
                key_token = shifted_map.get(key_token, None)
                if key_token is None:
                    return
            # Enforce max combos
            if self._combos_count >= MAX_COMBOS:
                # Stop silently when max reached
                self.stop()
                return
            # Insert delay if required
            now = time.monotonic()
            if self.use_real_delays:
                delta_ms = int((now - self._last_action_time) * 1000)
                if delta_ms > 0:
                    self._emit({'type': 'delay', 'ms': delta_ms})
            else:
                # default delay before next combo
                self._emit({'type': 'delay', 'ms': DEFAULT_DELAY_MS})
            self._last_action_time = now
            # Emit key action
            self._emit({'type': 'key', 'mods': self._pressed_mods, 'key': key_token, 'ts': now})
            self._combos_count += 1
        else:
            # key up: remove from pressed set
            self._pressed_keys.discard(name)

    def _name_to_token(self, name: str) -> Optional[str]:
        # Normalize numpad names
        if name.startswith('num '):
            rest = name[4:].strip()
            if rest.isdigit() and len(rest) == 1:
                return rest
        if name.startswith('numpad '):
            rest = name[7:].strip()
            if rest.isdigit() and len(rest) == 1:
                return rest
        if len(name) == 1:
            # Single char, return uppercase letter or digit
            if name.isalpha():
                return name.upper()
            if name.isdigit():
                return name
            # Allow shifted symbol keys to be handled upstream
            return name
        # Special names
        if name in SPECIAL_KEYS:
            return SPECIAL_KEYS[name]
        return None

    def _emit(self, action: Dict) -> None:
        cb = self.on_action
        if cb:
            try:
                cb(action)
            except Exception:
                pass

