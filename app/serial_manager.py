"""
Serial manager for MultiTap GUI.

Handles:
- Auto-detection of Arduino Micro COM port and connect/disconnect.
- Line-based protocol with Arduino.
- Signals to GUI for connection state, logs, macro data, and app triggers.

Protocol lines (newline-terminated UTF-8):
PC -> Arduino:
  - HELLO_PC
  - SET_MODE:<tap>:<1|2>
  - GET_MODE:<tap>
  - READ_MACRO:<tap>
  - WRITE_MACRO_BEGIN:<tap>
  -   A:K:<modmask>:<key>
  -   A:D:<ms>
  - WRITE_MACRO_END
  - SET_APP_CODE:<tap>:<code>   code in {F1..F12,Q1,Q2,Q3}
  - PROG_ACK                    only when Autorun is enabled
  - PROG_EXIT_ACK

Arduino -> PC:
  - HELLO_ARDUINO
  - MODE:<tap>:<1|2>
  - MACRO_BEGIN:<tap>
  -   A:K:<modmask>:<key>
  -   A:D:<ms>
  - MACRO_END:<tap>
  - OK | ERR:<reason>
  - TAP:<tap>
  - APP_TRIGGER:<tap>:<code>
  - PROG_REQ
  - PROG_EXIT_REQ

Where:
  - tap in {1,2,3}
  - modmask bitfield: 0x01=SHIFT, 0x02=CTRL, 0x04=ALT, 0x08=GUI
  - key in tokens: A..Z, 0..9, ESC, TAB, ENTER, SPACE, F1..F12, HOME, END,
                   PAGEUP, PAGEDOWN, LEFT, RIGHT, UP, DOWN, BACKSPACE, DELETE

This module defines SerialManager which runs a reader thread and emits Qt signals
via callbacks registered by the GUI.
"""
from __future__ import annotations

import threading
import time
import queue
import serial
from serial import Serial
from serial.tools import list_ports
from typing import Callable, Optional, List


SCAN_INTERVAL_SEC = 0.5
BAUDRATE = 115200
READ_TIMEOUT = 0.1   # seconds
WRITE_TIMEOUT = 2.5  # seconds (increase to avoid intermittent timeouts)


class SerialManager:
    """Manages serial connection and protocol with the Arduino device.

    This class is thread-safe where noted. Reading runs on a background thread
    that continuously reads lines and dispatches to callbacks.

    Callbacks (all optional):
      - on_log(str)
      - on_connected(port_name: str)
      - on_disconnected()
      - on_tap(tap: int)
      - on_app_trigger(tap: int, code: str)
      - on_macro_begin(tap: int)
      - on_macro_action(action_line: str)  # raw action line e.g. 'A:K:...'
      - on_macro_end(tap: int)
      - on_mode(tap: int, mode: int)
      - on_prog_req()                      # Arduino requests programming mode
      - on_prog_exit_req()                 # Arduino requests exit programming
      - on_ok()
      - on_err(reason: str)
    """

    def __init__(self) -> None:
        self._ser: Optional[Serial] = None
        self._port_name: Optional[str] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._outbox: "queue.Queue[str]" = queue.Queue()
        self._connected_once_notified = False
        self._autoscan_enabled = True
        self._device_preference: str = "auto"  # one of: 'auto', 'micro', 'esp32c3'
        # Callbacks
        self.on_log: Optional[Callable[[str], None]] = None
        self.on_connected: Optional[Callable[[str], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_tap: Optional[Callable[[int], None]] = None
        self.on_app_trigger: Optional[Callable[[int, str], None]] = None
        self.on_macro_begin: Optional[Callable[[int], None]] = None
        self.on_macro_action: Optional[Callable[[str], None]] = None
        self.on_macro_end: Optional[Callable[[int], None]] = None
        self.on_mode: Optional[Callable[[int, int], None]] = None
        self.on_prog_req: Optional[Callable[[], None]] = None
        self.on_prog_exit_req: Optional[Callable[[], None]] = None
        self.on_ok: Optional[Callable[[], None]] = None
        self.on_err: Optional[Callable[[str], None]] = None

    # ---------------------- Public API ----------------------
    def start(self) -> None:
        """Start the background reader thread.

        Safe to call multiple times; subsequent calls are ignored if already running.
        """
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._stop_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def stop(self) -> None:
        """Stop the background thread and close the serial port."""
        self._stop_event.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        self._reader_thread = None
        self._close_serial()

    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def current_port(self) -> Optional[str]:
        return self._port_name

    def set_autoscan_enabled(self, enabled: bool) -> None:
        """Enable/disable automatic scanning and connecting when disconnected."""
        self._autoscan_enabled = enabled

    def set_device_preference(self, preference: str) -> None:
        """Set preferred device type for autoscan: 'auto', 'micro', or 'esp32c3'."""
        pref = preference.lower().strip()
        if pref not in ("auto", "micro", "esp32c3"):
            pref = "auto"
        self._device_preference = pref

    def disconnect(self) -> None:
        """Manually disconnect from the current serial port, keeping the thread running."""
        was_connected = self.is_connected()
        pn = self._port_name
        self._close_serial()
        if was_connected:
            if self.on_log:
                self.on_log(f"Отключено от {pn}")
            if self.on_disconnected:
                self.on_disconnected()

    def send_line(self, line: str) -> None:
        """Queue a line to send over serial (thread-safe)."""
        if not line.endswith("\n"):
            line = line + "\n"
        self._outbox.put(line)

    def list_ports(self) -> List[str]:
        """List available port device names (e.g., COM3, /dev/ttyACM0)."""
        return [p.device for p in list_ports.comports()]

    def list_ports_with_desc(self) -> List[tuple[str, str]]:
        """List available ports as (device, description)."""
        result: List[tuple[str, str]] = []
        for p in list_ports.comports():
            result.append((p.device, p.description or ""))
        return result

    def try_connect_port(self, port_name: str) -> bool:
        """Attempt connection to the given port.

        Returns True on success, False otherwise.
        """
        try:
            ser = serial.Serial(
                port=port_name,
                baudrate=BAUDRATE,
                timeout=READ_TIMEOUT,
                write_timeout=WRITE_TIMEOUT,
                inter_byte_timeout=WRITE_TIMEOUT,
            )
            # Small delay to stabilize
            time.sleep(0.2)
            self._ser = ser
            self._port_name = port_name
            self._connected_once_notified = False
            self._write_now("HELLO_PC\n")
            if self.on_log:
                self.on_log(f"Подключено к {port_name}")
            if self.on_connected:
                self.on_connected(port_name)
            return True
        except Exception as e:
            if self.on_log:
                self.on_log(f"Не удалось подключиться к {port_name}: {e}")
            self._close_serial()
            return False

    # ---------------------- Internal ----------------------
    def _close_serial(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self._port_name = None

    def _reader_loop(self) -> None:
        """Main loop handling auto-scan, sending, and reading lines."""
        last_scan = 0.0
        buffer = b""
        notified_disconnect = False
        while not self._stop_event.is_set():
            now = time.time()
            # Auto-scan when not connected
            if self._ser is None or not self._ser.is_open:
                if now - last_scan >= SCAN_INTERVAL_SEC:
                    last_scan = now
                    if self._autoscan_enabled:
                        self._auto_scan_attempt()
                # Small sleep to avoid tight loop
                time.sleep(0.05)
                continue

            # Flush outgoing queue
            try:
                while True:
                    line = self._outbox.get_nowait()
                    self._write_now(line)
            except queue.Empty:
                pass

            # Read incoming
            try:
                data = self._ser.read(256)
                if data:
                    buffer += data
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        line_str = line.decode(errors="ignore").strip()
                        if line_str:
                            self._handle_line(line_str)
                            notified_disconnect = False
                else:
                    # no data; short sleep
                    time.sleep(0.01)
            except Exception as e:
                # Connection lost
                if self.on_log and not notified_disconnect:
                    self.on_log("Потеря связи с ардуино")
                if self.on_disconnected and not notified_disconnect:
                    self.on_disconnected()
                notified_disconnect = True
                self._close_serial()
                # Will auto-scan again
                time.sleep(0.25)

    def _auto_scan_attempt(self) -> None:
        """Scan ports based on preferred device and connect to the first match."""
        for p in list_ports.comports():
            desc = (p.description or "").lower()
            if self._desc_matches_preference(desc):
                self.try_connect_port(p.device)
                return

    def _desc_matches_preference(self, desc: str) -> bool:
        """Return True if the port description matches the selected device preference."""
        micro_match = ("arduino micro" in desc)
        esp_match = ("esp32" in desc) or ("esp32-c3" in desc) or ("arduino leonardo" in desc)
        if self._device_preference == "micro":
            return micro_match
        if self._device_preference == "esp32c3":
            return esp_match
        # auto: accept either (prefer micro in GUI preselect logic)
        return micro_match or esp_match

    def _write_now(self, line: str) -> None:
        if not self._ser or not self._ser.is_open:
            return
        if isinstance(line, str):
            data = line.encode()
        else:
            data = line
        try:
            self._ser.write(data)
            self._ser.flush()
        except Exception as e:
            if self.on_log:
                self.on_log(f"Ошибка отправки: {e}")

    def _handle_line(self, line: str) -> None:
        # Dispatch by prefixes
        if line == "HELLO_ARDUINO":
            self._write_now("HELLO_ACK\n")
            if self.on_log:
                self.on_log("Соединение установлено (HELLO)")
            return
        if line.startswith("MODE:"):
            try:
                _, tap_str, mode_str = line.split(":", 2)
                if self.on_mode:
                    self.on_mode(int(tap_str), int(mode_str))
            except Exception:
                pass
            return
        if line.startswith("MACRO_BEGIN:"):
            try:
                tap = int(line.split(":")[1])
                if self.on_macro_begin:
                    self.on_macro_begin(tap)
            except Exception:
                pass
            return
        if line.startswith("MACRO_END:"):
            try:
                tap = int(line.split(":")[1])
                if self.on_macro_end:
                    self.on_macro_end(tap)
            except Exception:
                pass
            return
        if line.startswith("A:"):
            if self.on_macro_action:
                self.on_macro_action(line)
            return
        if line.startswith("TAP:"):
            try:
                tap = int(line.split(":")[1])
                if self.on_tap:
                    self.on_tap(tap)
            except Exception:
                pass
            return
        if line.startswith("APP_TRIGGER:"):
            try:
                parts = line.split(":")
                tap = int(parts[1])
                code = parts[2]
                if self.on_app_trigger:
                    self.on_app_trigger(tap, code)
            except Exception:
                pass
            return
        if line == "PROG_REQ":
            if self.on_prog_req:
                self.on_prog_req()
            return
        if line == "PROG_EXIT_REQ":
            if self.on_prog_exit_req:
                self.on_prog_exit_req()
            return
        if line.startswith("PROG_EXIT_TAPS:"):
            try:
                taps = int(line.split(":")[1])
                # Reuse TAP callback to route target tap
                if self.on_tap:
                    self.on_tap(taps)
                if self.on_prog_exit_req:
                    self.on_prog_exit_req()
            except Exception:
                pass
            return
        if line == "OK":
            if self.on_ok:
                self.on_ok()
            return
        if line.startswith("ERR:"):
            if self.on_err:
                self.on_err(line[4:])
            return
        # Unknown line fallback
        if self.on_log:
            self.on_log(f"SERIAL: {line}")

