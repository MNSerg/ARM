"""
MultiTap GUI application (PyQt5) for controlling the Macro Button (Arduino Pro Micro).

Features:
- Dark theme, 600x700 window, tray minimize behavior.
- COM port selection with auto-scan every 0.5s for 'Arduino Micro' when disconnected.
- Autorun checkbox (default ON) controlling programming mode auto-record/ack.
- Tabs for single/double/triple tap.
- For each tap: radio 'Macro' or 'Application'.
- Macro mode: record via keyboard globally, list of actions with edit/move/delete, add delay, read/write/clear.
- Application mode: choose a file path for selected tap; when Arduino sends APP_TRIGGER, launches it.
- Console log at bottom with 'Clear console'.
- Settings persisted to 'Setting.ini' in working directory.

Constraints implemented:
- MAX_COMBOS = 20 for combinations; delays are separate items and editable.
- Default inter-combo delay is 10 ms if real delays are not used at record time.
- Logs print OK on successful write.
- Stops recording if device disconnects.

Note: Global keyboard capture requires the `keyboard` module and admin privileges
on some Windows systems.
"""
from __future__ import annotations

import os
import sys
import subprocess
from typing import List, Dict, Optional, Tuple

from PyQt5 import QtWidgets, QtGui, QtCore

# Support running as a module (python -m app) and as a script
try:
    from .serial_manager import SerialManager  # type: ignore
    from .macro_recorder import (
        MacroRecorder,
        DEFAULT_DELAY_MS,
        MAX_COMBOS,
        MOD_SHIFT,
        MOD_CTRL,
        MOD_ALT,
        MOD_GUI,
    )  # type: ignore
except Exception:  # pragma: no cover - fallback for direct script run
    from serial_manager import SerialManager  # type: ignore
    from macro_recorder import (  # type: ignore
        MacroRecorder,
        DEFAULT_DELAY_MS,
        MAX_COMBOS,
        MOD_SHIFT,
        MOD_CTRL,
        MOD_ALT,
        MOD_GUI,
    )


APP_NAME = "MultiTap"
SETTINGS_FILE = os.path.join(os.getcwd(), "Setting.ini")

# Key tokens allowed in actions (must match serial protocol)
KEY_TOKENS = set(
    [chr(c) for c in range(ord('A'), ord('Z') + 1)] +
    [str(d) for d in range(0,10)] +
    [f"F{i}" for i in range(1,13)] +
    ["ESC","TAB","ENTER","SPACE","HOME","END","PAGEUP","PAGEDOWN","LEFT","RIGHT","UP","DOWN","BACKSPACE","DELETE"]
)


class ActionItem:
    """Represents a single macro action (key combo or delay)."""
    def __init__(self, action_type: str, mods: int = 0, key: str = "", ms: int = 0) -> None:
        self.action_type = action_type  # 'key' or 'delay'
        self.mods = mods
        self.key = key
        self.ms = ms

    def to_serial_line(self) -> str:
        if self.action_type == 'key':
            return f"A:K:{self.mods}:{self.key}"
        else:
            return f"A:D:{self.ms}"

    def to_display(self) -> str:
        if self.action_type == 'delay':
            return f"Задержка {self.ms} мс"
        parts: List[str] = []
        if self.mods & MOD_GUI:
            parts.append("Win")
        if self.mods & MOD_CTRL:
            parts.append("Ctrl")
        if self.mods & MOD_SHIFT:
            parts.append("Shift")
        if self.mods & MOD_ALT:
            parts.append("Alt")
        parts.append(self.key)
        return "+".join(parts)


class TapConfig:
    """Per-tap configuration held in the GUI (mode, actions, app path)."""
    MODE_MACRO = 1
    MODE_APP = 2

    def __init__(self) -> None:
        self.mode = self.MODE_MACRO
        self.actions: List[ActionItem] = []
        self.app_path: str = ""

    def clear(self) -> None:
        self.actions.clear()

    def combos_count(self) -> int:
        return sum(1 for a in self.actions if a.action_type == 'key')


class Console(QtWidgets.QPlainTextEdit):
    """Simple console widget for logs."""
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(1000)

    def log(self, text: str) -> None:
        self.appendPlainText(text)


class MultiTapWindow(QtWidgets.QMainWindow):
    """Main application window."""

    # Signals for thread-safe updates
    sig_log = QtCore.pyqtSignal(str)
    sig_connected = QtCore.pyqtSignal(str)
    sig_disconnected = QtCore.pyqtSignal()
    sig_tap = QtCore.pyqtSignal(int)
    sig_app_trigger = QtCore.pyqtSignal(int, str)
    sig_macro_begin = QtCore.pyqtSignal(int)
    sig_macro_action = QtCore.pyqtSignal(str)
    sig_macro_end = QtCore.pyqtSignal(int)
    sig_mode = QtCore.pyqtSignal(int, int)
    sig_ok = QtCore.pyqtSignal()
    sig_err = QtCore.pyqtSignal(str)
    sig_prog_req = QtCore.pyqtSignal()
    sig_prog_exit_req = QtCore.pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(520, 560)
        self._apply_dark_theme()

        self.serial = SerialManager()
        self.serial.on_log = self.sig_log.emit
        self.serial.on_connected = self.sig_connected.emit
        self.serial.on_disconnected = self.sig_disconnected.emit
        self.serial.on_tap = self.sig_tap.emit
        self.serial.on_app_trigger = lambda t, c: self.sig_app_trigger.emit(t, c)
        self.serial.on_macro_begin = self.sig_macro_begin.emit
        self.serial.on_macro_action = self.sig_macro_action.emit
        self.serial.on_macro_end = self.sig_macro_end.emit
        self.serial.on_mode = lambda t, m: self.sig_mode.emit(t, m)
        self.serial.on_ok = self.sig_ok.emit
        self.serial.on_err = self.sig_err.emit
        self.serial.on_prog_req = self.sig_prog_req.emit
        self.serial.on_prog_exit_req = self.sig_prog_exit_req.emit

        self.console = Console()
        self._lost_reported = False

        # Settings stored in ini
        self.settings = QtCore.QSettings(SETTINGS_FILE, QtCore.QSettings.IniFormat)

        # UI
        self._central = QtWidgets.QWidget()
        self.setCentralWidget(self._central)
        self._vbox = QtWidgets.QVBoxLayout(self._central)

        self._build_top_bar()
        self._build_tabs()
        self._build_console()
        self._build_tray()
        self._build_status_bar()

        # State
        self.tap_configs = {
            1: TapConfig(),
            2: TapConfig(),
            3: TapConfig(),
            4: TapConfig(),
        }
        self.current_tap = 1
        self.recorder: Optional[MacroRecorder] = None
        self._reading_tap: Optional[int] = None
        self._prog_exit_pending: bool = False
        self._prog_buffer: Optional[List[ActionItem]] = None
        self._prog_source_tab: Optional[int] = None

        # Restore settings
        self._load_settings()
        # Apply settings after load
        if bool(self.settings.value("real_delays_default", False)):
            for tap in (1,2,3,4):
                self.cmb_delay_mode[tap].setCurrentIndex(1)
        if bool(self.settings.value("start_minimized", False)):
            QtCore.QTimer.singleShot(0, self.hide)
        # Apply device preference to serial autoscan
        device_pref = str(self.settings.value("device_preference", "auto"))
        self.serial.set_device_preference(device_pref)

        # Connect signals
        self._wire_signals()

        # Start serial manager
        self.serial.start()

    # --------------------- UI builders ---------------------
    def _apply_dark_theme(self) -> None:
        app = QtWidgets.QApplication.instance()
        palette = QtGui.QPalette()
        base_bg = QtGui.QColor(40, 40, 40)
        panel_bg = QtGui.QColor(30, 30, 30)
        btn_bg = QtGui.QColor(55, 55, 55)
        text_color = QtCore.Qt.white
        highlight = QtGui.QColor(45, 140, 240)

        palette.setColor(QtGui.QPalette.Window, base_bg)
        palette.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white)
        palette.setColor(QtGui.QPalette.Base, panel_bg)
        palette.setColor(QtGui.QPalette.AlternateBase, base_bg)
        palette.setColor(QtGui.QPalette.ToolTipBase, QtCore.Qt.white)
        palette.setColor(QtGui.QPalette.ToolTipText, QtCore.Qt.white)
        palette.setColor(QtGui.QPalette.Text, text_color)
        palette.setColor(QtGui.QPalette.Button, btn_bg)
        palette.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
        palette.setColor(QtGui.QPalette.BrightText, QtCore.Qt.red)
        palette.setColor(QtGui.QPalette.Highlight, highlight)
        palette.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
        app.setPalette(palette)

        # StyleSheet to enforce dark tabs, group boxes, and list
        app.setStyleSheet(
            """
            QTabWidget::pane { border: 1px solid #333; background: #202020; }
            QTabBar::tab { background: #2d2d2d; color: #ddd; padding: 6px 12px; }
            QTabBar::tab:selected { background: #3a3a3a; }
            QGroupBox { border: 1px solid #333; margin-top: 8px; background: #1e1e1e; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
            QListWidget { background: #1e1e1e; color: #eee; }
            QPlainTextEdit { background: #111; color: #ddd; }
            QPushButton { background-color: #383838; color: #eee; border: 1px solid #444; padding: 6px 10px; }
            QPushButton:hover { background-color: #444; }
            QLineEdit { background: #242424; color: #eee; border: 1px solid #444; }
            QComboBox { background: #242424; color: #eee; border: 1px solid #444; }
            QMenu { background: #2b2b2b; color: #eee; }
            QMenu::item:selected { background: #3a3a3a; }
            QCheckBox { color: #eee; }
            QLabel { color: #eee; }
            """
        )

        font = QtGui.QFont()
        font.setPointSize(10)
        app.setFont(font)

    def _build_top_bar(self) -> None:
        h = QtWidgets.QHBoxLayout()
        self._vbox.addLayout(h)

        self.cb_ports = QtWidgets.QComboBox()
        self.btn_refresh = QtWidgets.QPushButton("Обновить")
        self.btn_connect = QtWidgets.QPushButton("Подключить")
        self.chk_autorun = QtWidgets.QCheckBox("Авторежим")
        self.chk_autorun.setChecked(True)
        self.btn_settings = QtWidgets.QPushButton("Настройки")

        h.addWidget(QtWidgets.QLabel("COM порт:"))
        h.addWidget(self.cb_ports, 1)
        h.addWidget(self.btn_refresh)
        h.addWidget(self.btn_connect)
        h.addWidget(self.chk_autorun)
        h.addStretch(1)
        h.addWidget(self.btn_settings)

        self.btn_refresh.clicked.connect(self._refresh_ports)
        self.btn_connect.clicked.connect(self._toggle_connection)
        self.chk_autorun.toggled.connect(self._on_autorun_toggled)
        self.btn_settings.clicked.connect(self._open_settings)

        self._refresh_ports()

    def _build_tabs(self) -> None:
        self.tabs = QtWidgets.QTabWidget()
        self._vbox.addWidget(self.tabs, 1)

        self.pages: Dict[int, QtWidgets.QWidget] = {}
        for tap, title in [(1, "Одиночный тап"), (2, "Двойной тап"), (3, "Тройной тап"), (4, "Четверной тап")]:
            w = QtWidgets.QWidget()
            self.pages[tap] = w
            self._build_tap_page(w, tap)
            self.tabs.addTab(w, title)

        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _build_tap_page(self, w: QtWidgets.QWidget, tap: int) -> None:
        v = QtWidgets.QVBoxLayout(w)

        # Mode radios
        mode_group = QtWidgets.QGroupBox("Режим")
        v.addWidget(mode_group)
        h = QtWidgets.QHBoxLayout(mode_group)
        rb_macro = QtWidgets.QRadioButton("Макрос")
        rb_app = QtWidgets.QRadioButton("Приложение")
        rb_macro.setChecked(True)
        h.addWidget(rb_macro)
        h.addWidget(rb_app)

        # Macro controls
        macro_group = QtWidgets.QGroupBox("Последовательность действий:")
        v.addWidget(macro_group, 2)
        mv = QtWidgets.QVBoxLayout(macro_group)

        self.lst_actions = getattr(self, 'lst_actions', None)
        if self.lst_actions is None:
            self.lst_actions = {}
        lst = QtWidgets.QListWidget()
        self.lst_actions[tap] = lst
        mv.addWidget(lst, 1)
        # Context menu for actions list
        lst.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        lst.customContextMenuRequested.connect(lambda pos, t=tap: self._show_actions_context_menu(t, pos))

        rec_h = QtWidgets.QHBoxLayout()
        mv.addLayout(rec_h)
        self.btn_start = getattr(self, 'btn_start', None)
        if self.btn_start is None:
            self.btn_start = {}
        self.btn_stop = getattr(self, 'btn_stop', None)
        if self.btn_stop is None:
            self.btn_stop = {}
        btn_start = QtWidgets.QPushButton("Начать запись")
        btn_stop = QtWidgets.QPushButton("Остановить запись")
        self.btn_start[tap] = btn_start
        self.btn_stop[tap] = btn_stop
        rec_h.addWidget(btn_start)
        rec_h.addWidget(btn_stop)

        delay_h = QtWidgets.QHBoxLayout()
        mv.addLayout(delay_h)
        delay_h.addWidget(QtWidgets.QLabel("Задержки при записи:"))
        self.cmb_delay_mode = getattr(self, 'cmb_delay_mode', None)
        if self.cmb_delay_mode is None:
            self.cmb_delay_mode = {}
        cmb_delay = QtWidgets.QComboBox()
        cmb_delay.addItems(["10 мс (по-умолчанию)", "Реальные задержки"])
        self.cmb_delay_mode[tap] = cmb_delay
        delay_h.addWidget(cmb_delay)

        # Collapsible edit menu (three-dots)
        edit_h = QtWidgets.QHBoxLayout()
        mv.addLayout(edit_h)
        self.btn_read = getattr(self, 'btn_read', None)
        self.btn_write = getattr(self, 'btn_write', None)
        self.btn_clear = getattr(self, 'btn_clear', None)
        self.btn_edit = getattr(self, 'btn_edit', None)
        self.btn_add_key = getattr(self, 'btn_add_key', None)
        self.btn_up = getattr(self, 'btn_up', None)
        self.btn_down = getattr(self, 'btn_down', None)
        self.btn_del = getattr(self, 'btn_del', None)
        self.btn_add_delay = getattr(self, 'btn_add_delay', None)
        if self.btn_read is None:
            self.btn_read = {}
            self.btn_write = {}
            self.btn_clear = {}
            self.btn_edit = {}
            self.btn_add_key = {}
            self.btn_up = {}
            self.btn_down = {}
            self.btn_del = {}
            self.btn_add_delay = {}
        btn_read = QtWidgets.QPushButton("Считать")
        btn_write = QtWidgets.QPushButton("Записать")
        btn_clear = QtWidgets.QPushButton("Очистить")
        btn_edit = QtWidgets.QPushButton("Редактировать")
        btn_add_key = QtWidgets.QPushButton("Добавить клавишу")
        btn_up = QtWidgets.QPushButton("Вверх")
        btn_down = QtWidgets.QPushButton("Вниз")
        btn_del = QtWidgets.QPushButton("Удалить действие")
        btn_add_delay = QtWidgets.QPushButton("Добавить задержку")
        self.btn_read[tap] = btn_read
        self.btn_write[tap] = btn_write
        self.btn_clear[tap] = btn_clear
        self.btn_edit[tap] = btn_edit
        self.btn_add_key[tap] = btn_add_key
        self.btn_up[tap] = btn_up
        self.btn_down[tap] = btn_down
        self.btn_del[tap] = btn_del
        self.btn_add_delay[tap] = btn_add_delay
        # Place heavy edit actions into a menu button to save space
        menu_btn = QtWidgets.QToolButton()
        menu_btn.setText("...")
        menu_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu = QtWidgets.QMenu(menu_btn)
        menu.addAction("Редактировать", lambda: btn_edit.click())
        menu.addAction("Добавить клавишу", lambda: btn_add_key.click())
        menu.addAction("Вверх", lambda: btn_up.click())
        menu.addAction("Вниз", lambda: btn_down.click())
        menu.addAction("Удалить действие", lambda: btn_del.click())
        menu.addAction("Добавить задержку", lambda: btn_add_delay.click())
        menu_btn.setMenu(menu)

        # Keep primary actions visible
        edit_h.addWidget(btn_read)
        edit_h.addWidget(btn_write)
        edit_h.addWidget(btn_clear)
        edit_h.addWidget(menu_btn)

        # App controls
        app_group = QtWidgets.QGroupBox("Приложение для запуска")
        v.addWidget(app_group)
        ah = QtWidgets.QHBoxLayout(app_group)
        self.ed_app_path = getattr(self, 'ed_app_path', None)
        self.btn_browse = getattr(self, 'btn_browse', None)
        if self.ed_app_path is None:
            self.ed_app_path = {}
            self.btn_browse = {}
        ed = QtWidgets.QLineEdit()
        ed.setPlaceholderText("Путь к приложению...")
        btn_browse = QtWidgets.QPushButton("Обзор")
        self.ed_app_path[tap] = ed
        self.btn_browse[tap] = btn_browse
        ah.addWidget(ed, 1)
        ah.addWidget(btn_browse)

        # Wire per-tap controls
        rb_macro.toggled.connect(lambda checked, t=tap: self._on_mode_changed(t, checked))
        btn_start.clicked.connect(lambda _=False, t=tap: self._start_record(t))
        btn_stop.clicked.connect(lambda _=False, t=tap: self._stop_record(t))
        btn_read.clicked.connect(lambda _=False, t=tap: self._read_macro(t))
        btn_write.clicked.connect(lambda _=False, t=tap: self._write_macro(t))
        btn_clear.clicked.connect(lambda _=False, t=tap: self._clear_actions(t))
        btn_edit.clicked.connect(lambda _=False, t=tap: self._edit_action(t))
        btn_up.clicked.connect(lambda _=False, t=tap: self._move_action(t, -1))
        btn_down.clicked.connect(lambda _=False, t=tap: self._move_action(t, +1))
        btn_del.clicked.connect(lambda _=False, t=tap: self._delete_action(t))
        btn_add_delay.clicked.connect(lambda _=False, t=tap: self._add_delay(t))
        btn_browse.clicked.connect(lambda _=False, t=tap: self._browse_app(t))
        btn_add_key.clicked.connect(lambda _=False, t=tap: self._add_key(t))

        # Store mode radio for this tap on the widget for retrieval
        w.rb_macro = rb_macro  # type: ignore[attr-defined]
        w.rb_app = rb_app      # type: ignore[attr-defined]

    def _build_console(self) -> None:
        # Toggleable console area
        h = QtWidgets.QHBoxLayout()
        self._vbox.addLayout(h)
        self.btn_toggle_console = QtWidgets.QPushButton("Показать консоль")
        self.btn_toggle_console.setCheckable(True)
        self.btn_toggle_console.setChecked(False)
        self.btn_toggle_console.toggled.connect(self._toggle_console)
        btn_clear = QtWidgets.QPushButton("Очистить консоль")
        btn_clear.clicked.connect(lambda: self.console.setPlainText(""))
        h.addWidget(self.btn_toggle_console)
        h.addWidget(btn_clear)
        self._vbox.addWidget(self.console, 1)
        self.console.setMaximumHeight(140)
        self.console.setVisible(False)

    def _build_status_bar(self) -> None:
        bar = QtWidgets.QHBoxLayout()
        self._vbox.addLayout(bar)
        self.lbl_conn = QtWidgets.QLabel("Не подключен")
        self.lbl_op = QtWidgets.QLabel("")
        self.lbl_rec = QtWidgets.QLabel("Запись: выкл")
        bar.addWidget(self.lbl_conn)
        bar.addStretch(1)
        bar.addWidget(self.lbl_rec)
        bar.addSpacing(24)
        bar.addWidget(QtWidgets.QLabel("Статус:"))
        bar.addWidget(self.lbl_op)

    def _build_tray(self) -> None:
        self.tray = QtWidgets.QSystemTrayIcon(self)
        icon = self.style().standardIcon(QtWidgets.QStyle.SP_ComputerIcon)
        self.tray.setIcon(icon)
        menu = QtWidgets.QMenu()
        act_show = menu.addAction("Показать")
        act_exit = menu.addAction("Выход")
        act_show.triggered.connect(self._restore_from_tray)
        act_exit.triggered.connect(self._exit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    # --------------------- Signal wiring ---------------------
    def _wire_signals(self) -> None:
        self.sig_log.connect(self.console.log)
        self.sig_connected.connect(self._on_connected)
        self.sig_disconnected.connect(self._on_disconnected)
        self.sig_tap.connect(self._on_tap)
        self.sig_app_trigger.connect(self._on_app_trigger)
        self.sig_macro_begin.connect(self._on_macro_begin)
        self.sig_macro_action.connect(self._on_macro_action)
        self.sig_macro_end.connect(self._on_macro_end)
        self.sig_mode.connect(self._on_mode_from_device)
        self.sig_ok.connect(self._on_ok)
        self.sig_err.connect(self._on_err)
        self.sig_prog_req.connect(self._on_prog_req)
        self.sig_prog_exit_req.connect(self._on_prog_exit_req)

    # --------------------- Event handlers ---------------------
    def _refresh_ports(self) -> None:
        self.cb_ports.clear()
        # Fill with device (description)
        preferred_index = -1
        i = 0
        for dev, desc in self.serial.list_ports_with_desc():
            label = f"{dev} ({desc})" if desc else dev
            self.cb_ports.addItem(label, dev)
            d = (desc or '').lower()
            pref = str(self.settings.value("device_preference", "auto")).lower()
            if pref == 'micro' and 'arduino micro' in d:
                preferred_index = i
            elif pref == 'esp32c3' and (('esp32' in d) or ('esp32-c3' in d) or ('arduino leonardo' in d)):
                preferred_index = i
            elif pref == 'auto' and preferred_index < 0:
                # choose micro first if seen, otherwise any esp32
                if 'arduino micro' in d:
                    preferred_index = i
                elif (('esp32' in d) or ('esp32-c3' in d) or ('arduino leonardo' in d)):
                    preferred_index = i
            i += 1
        if preferred_index >= 0:
            self.cb_ports.setCurrentIndex(preferred_index)

    def _connect_selected(self) -> None:
        # Current data holds the actual device path
        port = self.cb_ports.currentData()
        if port:
            if self.serial.try_connect_port(port):
                self.btn_connect.setText("Отключить")

    def _on_tab_changed(self, index: int) -> None:
        self.current_tap = index + 1

    def _on_connected(self, port_name: str) -> None:
        self.console.log(f"Подключено: {port_name}")
        self._lost_reported = False
        self.btn_connect.setText("Отключить")
        self.lbl_conn.setText("Подключен")
        # Apply GUI-configured modes to device
        for tap in (1, 2, 3, 4):
            mode = self.tap_configs[tap].mode
            self.serial.send_line(f"SET_MODE:{tap}:{mode}")
            if mode == TapConfig.MODE_APP:
                code = f"Q{tap}"  # stable codes Q1/Q2/Q3
                self.serial.send_line(f"SET_APP_CODE:{tap}:{code}")

    def _on_disconnected(self) -> None:
        if not self._lost_reported:
            self.console.log("Потеря связи")
            self._lost_reported = True
        self.btn_connect.setText("Подключить")
        self.lbl_conn.setText("Не подключен")
        # Stop recording if active
        if self.recorder is not None:
            self._stop_record(self.current_tap)

    def _toggle_connection(self) -> None:
        if self.serial.is_connected():
            self.serial.disconnect()
            self.btn_connect.setText("Подключить")
        else:
            self._connect_selected()

    def _on_autorun_toggled(self, checked: bool) -> None:
        # Autorun also controls autoscan/auto-connect behavior
        self.serial.set_autoscan_enabled(checked)

    def _on_app_trigger(self, tap: int, code: str) -> None:
        self.console.log(f"APP_TRIGGER tap={tap} code={code}")
        path = self.tap_configs.get(tap, TapConfig()).app_path
        if path and os.path.exists(path):
            try:
                # Start detached process
                if sys.platform.startswith('win'):
                    os.startfile(path)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen([path], start_new_session=True)
                self.console.log("Запуск приложения: OK")
            except Exception as e:
                self.console.log(f"Ошибка запуска приложения: {e}")
        else:
            self.console.log("Путь к приложению не задан или не существует")

    def _on_mode_from_device(self, tap: int, mode: int) -> None:
        cfg = self.tap_configs[tap]
        cfg.mode = mode
        page = self.pages[tap]
        page.rb_macro.setChecked(mode == TapConfig.MODE_MACRO)  # type: ignore[attr-defined]
        page.rb_app.setChecked(mode == TapConfig.MODE_APP)      # type: ignore[attr-defined]
        self.console.log(f"Режим тапа {tap}: {'Макрос' if mode==1 else 'Приложение'}")
        self.lbl_op.setText("ОК")

    def _on_macro_begin(self, tap: int) -> None:
        self._reading_tap = tap
        self.tap_configs[tap].clear()
        self._refresh_actions_list(tap)

    def _on_macro_action(self, line: str) -> None:
        try:
            parts = line.split(":")
            if parts[1] == 'K':
                mods = int(parts[2])
                key = parts[3]
                target_tap = self._reading_tap or self.current_tap
                self.tap_configs[target_tap].actions.append(ActionItem('key', mods=mods, key=key))
            elif parts[1] == 'D':
                ms = int(parts[2])
                target_tap = self._reading_tap or self.current_tap
                self.tap_configs[target_tap].actions.append(ActionItem('delay', ms=ms))
            self._refresh_actions_list(self._reading_tap or self.current_tap)
        except Exception:
            pass

    def _on_macro_end(self, tap: int) -> None:
        self.console.log(f"Считан макрос для тапа {tap}")
        self._reading_tap = None
        self.lbl_op.setText("ОК")

    def _on_prog_req(self) -> None:
        self.console.log("Вход в режим программирования запрошен")
        # Всегда подтверждаем вход в режим программирования, чтобы LED загорелся
        self.serial.send_line("PROG_ACK")
        if self.chk_autorun.isChecked():
            # Prepare programming buffer and start recording from current tab
            self._prog_buffer = []
            self._prog_source_tab = self.current_tap
            self._start_record(self.current_tap)
            # Mark that next TAP decides the target tap on exit
            self._prog_exit_pending = True

    def _on_prog_exit_req(self) -> None:
        self.console.log("Выход из режима программирования запрошен")
        self.serial.send_line("PROG_EXIT_ACK")
        if self.recorder is not None:
            # Stop recording
            self._stop_record(self.current_tap)
            target_tap = self.current_tap
            # If we have a programming buffer, write it to the target tap and copy to GUI list
            if self._prog_buffer is not None and len(self._prog_buffer) > 0:
                # Copy buffer into target tap list for UI consistency
                self.tap_configs[target_tap].actions = [ActionItem(a.action_type, a.mods, a.key, a.ms) for a in self._prog_buffer]
                self._refresh_actions_list(target_tap)
                # Send SET_MODE to ensure device is in macro mode before write
                self.serial.send_line(f"SET_MODE:{target_tap}:{TapConfig.MODE_MACRO}")
                self._write_macro_actions(target_tap, self._prog_buffer)
            else:
                # Fallback: write current tap actions
                self._write_macro(target_tap)
        # Reset programming state
        self._prog_exit_pending = False
        self._prog_buffer = None
        self._prog_source_tab = None

    # --------------------- Mode and actions ---------------------
    def _on_mode_changed(self, tap: int, macro_checked: bool) -> None:
        cfg = self.tap_configs[tap]
        cfg.mode = TapConfig.MODE_MACRO if macro_checked else TapConfig.MODE_APP
        self.serial.send_line(f"SET_MODE:{tap}:{cfg.mode}")
        if cfg.mode == TapConfig.MODE_APP:
            code = f"Q{tap}"
            self.serial.send_line(f"SET_APP_CODE:{tap}:{code}")

    def _start_record(self, tap: int) -> None:
        if self.recorder is not None:
            self.console.log("Запись уже активна")
            return
        # Clear list per spec
        cfg = self.tap_configs[tap]
        cfg.clear()
        self._refresh_actions_list(tap)
        use_real = self.cmb_delay_mode[tap].currentIndex() == 1
        rec = MacroRecorder(use_real_delays=use_real)
        self.recorder = rec

        def on_action(action: Dict) -> None:
            if action['type'] == 'delay':
                delay_action = ActionItem('delay', ms=int(action['ms']))
                cfg.actions.append(delay_action)
                if self._prog_buffer is not None:
                    self._prog_buffer.append(ActionItem('delay', ms=delay_action.ms))
            elif action['type'] == 'key':
                # Enforce key tokens
                key = action['key']
                if key not in KEY_TOKENS:
                    return
                if cfg.combos_count() >= MAX_COMBOS:
                    return
                key_action = ActionItem('key', mods=int(action['mods']), key=key)
                cfg.actions.append(key_action)
                if self._prog_buffer is not None:
                    self._prog_buffer.append(ActionItem('key', mods=key_action.mods, key=key_action.key))
            self._refresh_actions_list(tap)

        rec.on_action = on_action
        rec.on_stop = lambda: (self.console.log("Запись завершена"), self.lbl_rec.setText("Запись: выкл"))
        rec.start()
        self.console.log("Запись начата")
        self.lbl_rec.setText("Запись: вкл")

    def _stop_record(self, tap: int) -> None:
        if self.recorder is None:
            return
        self.recorder.stop()
        self.recorder = None
        self.lbl_rec.setText("Запись: выкл")

    def _refresh_actions_list(self, tap: int) -> None:
        lst = self.lst_actions[tap]
        lst.clear()
        for a in self.tap_configs[tap].actions:
            lst.addItem(a.to_display())

    def _selected_action_index(self, tap: int) -> int:
        lst = self.lst_actions[tap]
        sel = lst.selectedIndexes()
        return sel[0].row() if sel else -1

    def _edit_action(self, tap: int) -> None:
        idx = self._selected_action_index(tap)
        if idx < 0:
            return
        a = self.tap_configs[tap].actions[idx]
        if a.action_type == 'delay':
            ms, ok = QtWidgets.QInputDialog.getInt(self, "Изменить задержку", "мс:", value=a.ms, min=0, max=10000)
            if ok:
                a.ms = ms
        else:
            dlg = self._key_edit_dialog(a)
            if dlg.exec_() == QtWidgets.QDialog.Accepted:
                mods, key = dlg.result_mods, dlg.result_key
                a.mods, a.key = mods, key
        self._refresh_actions_list(tap)

    def _key_edit_dialog(self, a: ActionItem) -> QtWidgets.QDialog:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Редактировать клавишу")
        v = QtWidgets.QVBoxLayout(dlg)
        chk_shift = QtWidgets.QCheckBox("Shift"); chk_shift.setChecked(bool(a.mods & MOD_SHIFT))
        chk_ctrl = QtWidgets.QCheckBox("Ctrl"); chk_ctrl.setChecked(bool(a.mods & MOD_CTRL))
        chk_alt = QtWidgets.QCheckBox("Alt"); chk_alt.setChecked(bool(a.mods & MOD_ALT))
        chk_gui = QtWidgets.QCheckBox("Win"); chk_gui.setChecked(bool(a.mods & MOD_GUI))
        v.addWidget(chk_shift); v.addWidget(chk_ctrl); v.addWidget(chk_alt); v.addWidget(chk_gui)
        cmb_key = QtWidgets.QComboBox()
        keys_sorted = sorted(KEY_TOKENS)
        cmb_key.addItems(keys_sorted)
        if a.key in KEY_TOKENS:
            cmb_key.setCurrentText(a.key)
        v.addWidget(QtWidgets.QLabel("Клавиша:"))
        v.addWidget(cmb_key)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        v.addWidget(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        dlg.result_mods = 0  # type: ignore[attr-defined]
        dlg.result_key = a.key  # type: ignore[attr-defined]
        def on_accept() -> None:
            mods = 0
            if chk_shift.isChecked(): mods |= MOD_SHIFT
            if chk_ctrl.isChecked(): mods |= MOD_CTRL
            if chk_alt.isChecked(): mods |= MOD_ALT
            if chk_gui.isChecked(): mods |= MOD_GUI
            dlg.result_mods = mods  # type: ignore[attr-defined]
            dlg.result_key = cmb_key.currentText()  # type: ignore[attr-defined]
        btns.accepted.connect(on_accept)
        return dlg

    def _move_action(self, tap: int, delta: int) -> None:
        idx = self._selected_action_index(tap)
        if idx < 0:
            return
        actions = self.tap_configs[tap].actions
        j = max(0, min(len(actions) - 1, idx + delta))
        if j == idx:
            return
        actions[idx], actions[j] = actions[j], actions[idx]
        self._refresh_actions_list(tap)
        self.lst_actions[tap].setCurrentRow(j)

    def _delete_action(self, tap: int) -> None:
        idx = self._selected_action_index(tap)
        if idx < 0:
            return
        del self.tap_configs[tap].actions[idx]
        self._refresh_actions_list(tap)

    def _add_delay(self, tap: int) -> None:
        ms, ok = QtWidgets.QInputDialog.getInt(self, "Добавить задержку", "мс:", value=DEFAULT_DELAY_MS, min=0, max=10000)
        if not ok:
            return
        idx = self._selected_action_index(tap)
        a = ActionItem('delay', ms=ms)
        if idx < 0:
            self.tap_configs[tap].actions.append(a)
        else:
            self.tap_configs[tap].actions.insert(idx + 1, a)
        self._refresh_actions_list(tap)

    def _clear_actions(self, tap: int) -> None:
        self.tap_configs[tap].clear()
        self._refresh_actions_list(tap)

    def _read_macro(self, tap: int) -> None:
        # Request current mode and macro
        self.serial.send_line(f"GET_MODE:{tap}")
        self.serial.send_line(f"READ_MACRO:{tap}")

    def _write_macro(self, tap: int) -> None:
        # Enforce MAX_COMBOS
        combos = [a for a in self.tap_configs[tap].actions if a.action_type == 'key']
        if len(combos) > MAX_COMBOS:
            self.console.log(f"Ошибка: более {MAX_COMBOS} комбинаций")
            return
        self.serial.send_line(f"WRITE_MACRO_BEGIN:{tap}")
        for a in self.tap_configs[tap].actions:
            self.serial.send_line(a.to_serial_line())
        self.serial.send_line("WRITE_MACRO_END")

    def _write_macro_actions(self, tap: int, actions: List[ActionItem]) -> None:
        # Enforce MAX_COMBOS
        combos = [a for a in actions if a.action_type == 'key']
        if len(combos) > MAX_COMBOS:
            self.console.log(f"Ошибка: более {MAX_COMBOS} комбинаций")
            return
        self.serial.send_line(f"WRITE_MACRO_BEGIN:{tap}")
        for a in actions:
            # Validate token before sending to device
            if a.action_type == 'key' and a.key not in KEY_TOKENS:
                continue
            self.serial.send_line(a.to_serial_line())
        self.serial.send_line("WRITE_MACRO_END")

    def _browse_app(self, tap: int) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Выбрать приложение", "", "Все файлы (*)")
        if path:
            self.ed_app_path[tap].setText(path)
            self.tap_configs[tap].app_path = path
            # Inform device of app mode if selected
            page = self.pages[tap]
            if page.rb_app.isChecked():  # type: ignore[attr-defined]
                self.serial.send_line(f"SET_MODE:{tap}:{TapConfig.MODE_APP}")
                self.serial.send_line(f"SET_APP_CODE:{tap}:Q{tap}")

    def _open_settings(self) -> None:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Настройки")
        v = QtWidgets.QVBoxLayout(dlg)
        chk_autostart = QtWidgets.QCheckBox("Запускать MultiTap вместе с Windows")
        current = bool(self.settings.value("autostart_windows", False))
        chk_autostart.setChecked(current)
        v.addWidget(chk_autostart)

        # Additional settings
        chk_start_minimized = QtWidgets.QCheckBox("Запускать свернутым в трей")
        chk_start_minimized.setChecked(bool(self.settings.value("start_minimized", False)))
        v.addWidget(chk_start_minimized)

        chk_real_delays_default = QtWidgets.QCheckBox("По умолчанию реальные задержки при записи")
        chk_real_delays_default.setChecked(bool(self.settings.value("real_delays_default", False)))
        v.addWidget(chk_real_delays_default)

        # Device preference
        device_pref_label = QtWidgets.QLabel("Устройство для автопоиска:")
        cmb_device_pref = QtWidgets.QComboBox()
        cmb_device_pref.addItems(["Авто", "Arduino Micro", "ESP32-C3 mini"])
        saved_pref = str(self.settings.value("device_preference", "auto")).lower()
        if saved_pref == 'micro':
            cmb_device_pref.setCurrentIndex(1)
        elif saved_pref == 'esp32c3':
            cmb_device_pref.setCurrentIndex(2)
        else:
            cmb_device_pref.setCurrentIndex(0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(device_pref_label)
        row.addWidget(cmb_device_pref)
        v.addLayout(row)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        v.addWidget(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            enabled = chk_autostart.isChecked()
            self.settings.setValue("autostart_windows", enabled)
            self.settings.setValue("start_minimized", chk_start_minimized.isChecked())
            self.settings.setValue("real_delays_default", chk_real_delays_default.isChecked())
            # Save device preference
            idx = cmb_device_pref.currentIndex()
            pref = 'auto' if idx == 0 else ('micro' if idx == 1 else 'esp32c3')
            self.settings.setValue("device_preference", pref)
            self.settings.sync()
            # Apply immediately
            self._apply_windows_autostart(enabled)
            self.serial.set_device_preference(pref)
            self._refresh_ports()
            # Update delay mode combos on all tabs to reflect the new default immediately
            use_real = chk_real_delays_default.isChecked()
            for t in (1, 2, 3, 4):
                if t in self.cmb_delay_mode:
                    self.cmb_delay_mode[t].setCurrentIndex(1 if use_real else 0)

    def _apply_windows_autostart(self, enable: bool) -> None:
        if sys.platform.startswith('win'):
            try:
                import winreg  # type: ignore
                run_key = r"Software\\Microsoft\\Windows\\CurrentVersion\\Run"
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
                    app_name = APP_NAME
                    if enable:
                        exe = sys.executable
                        cmd = f'"{exe}" -m app'
                        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
                    else:
                        try:
                            winreg.DeleteValue(key, app_name)
                        except FileNotFoundError:
                            pass
            except Exception as e:
                self.console.log(f"Не удалось обновить автозапуск: {e}")

    def _add_key(self, tap: int) -> None:
        # Add a new key action interactively
        a = ActionItem('key', mods=0, key='A')
        dlg = self._key_edit_dialog(a)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            mods, key = dlg.result_mods, dlg.result_key
            if key not in KEY_TOKENS:
                return
            idx = self._selected_action_index(tap)
            new_action = ActionItem('key', mods=mods, key=key)
            if idx < 0:
                self.tap_configs[tap].actions.append(new_action)
            else:
                self.tap_configs[tap].actions.insert(idx + 1, new_action)
            self._refresh_actions_list(tap)

    def _show_actions_context_menu(self, tap: int, pos: QtCore.QPoint) -> None:
        lst = self.lst_actions[tap]
        menu = QtWidgets.QMenu(lst)
        menu.addAction("Редактировать", lambda: self._edit_action(tap))
        menu.addAction("Вверх", lambda: self._move_action(tap, -1))
        menu.addAction("Вниз", lambda: self._move_action(tap, +1))
        menu.addAction("Удалить", lambda: self._delete_action(tap))
        menu.exec_(lst.mapToGlobal(pos))

    def _toggle_console(self, checked: bool) -> None:
        self.console.setVisible(checked)
        self.btn_toggle_console.setText("Скрыть консоль" if checked else "Показать консоль")

    def _on_ok(self) -> None:
        self.console.log("OK")
        self.lbl_op.setText("ОК")

    def _on_err(self, reason: str) -> None:
        self.console.log(f"ERR: {reason}")
        self.lbl_op.setText("Ошибка")

    def _on_tap(self, tap: int) -> None:
        self.console.log(f"Тап: {tap}")
        # When exiting programming mode, remember which tap to write macro to
        if getattr(self, '_prog_exit_pending', False):
            if tap in (1, 2, 3, 4):
                self.current_tap = tap

    # --------------------- Settings ---------------------
    def _load_settings(self) -> None:
        # Autorun
        self.chk_autorun.setChecked(self.settings.value("autorun", True, type=bool))
        # App paths and modes
        for tap in (1,2,3,4):
            mode = int(self.settings.value(f"tap{tap}/mode", TapConfig.MODE_MACRO))
            self.tap_configs[tap].mode = mode
            page = self.pages[tap]
            page.rb_macro.setChecked(mode == TapConfig.MODE_MACRO)  # type: ignore[attr-defined]
            page.rb_app.setChecked(mode == TapConfig.MODE_APP)      # type: ignore[attr-defined]
            app_path = self.settings.value(f"tap{tap}/app_path", "", type=str)
            self.tap_configs[tap].app_path = app_path
            self.ed_app_path[tap].setText(app_path)

    def _save_settings(self) -> None:
        self.settings.setValue("autorun", self.chk_autorun.isChecked())
        for tap in (1,2,3,4):
            self.settings.setValue(f"tap{tap}/mode", self.tap_configs[tap].mode)
            self.settings.setValue(f"tap{tap}/app_path", self.tap_configs[tap].app_path)
        self.settings.sync()

    # --------------------- Tray / close behavior ---------------------
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore[override]
        # Close completely on X per requirement
        self._save_settings()
        self.serial.stop()
        event.accept()

    def changeEvent(self, event: QtCore.QEvent) -> None:  # type: ignore[override]
        if event.type() == QtCore.QEvent.WindowStateChange:
            if self.isMinimized():
                QtCore.QTimer.singleShot(0, self.hide)
                self.tray.showMessage(APP_NAME, "Свёрнуто в трей", QtWidgets.QSystemTrayIcon.Information, 2000)
        super().changeEvent(event)

    def _on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QtWidgets.QSystemTrayIcon.Trigger, QtWidgets.QSystemTrayIcon.DoubleClick):
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.activateWindow()

    def _exit_app(self) -> None:
        self._save_settings()
        QtWidgets.QApplication.instance().quit()


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    win = MultiTapWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
