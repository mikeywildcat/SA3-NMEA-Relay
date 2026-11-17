"""System tray icon (Windows) / menu bar icon (macOS) for SA3 NMEA Relay.

Responsibilities:
  - Show/hide the main window via tray icon click or context menu.
  - Provide a "Start at Login" toggle that is persisted via platform APIs:
      macOS   → ~/Library/LaunchAgents/com.sa3nmearelay.plist
      Windows → HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
"""

from __future__ import annotations

import logging
import platform
import sys
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QSettings, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QImage, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from translations import tr

_logger = logging.getLogger(__name__)

_APP_NAME = "SA3NMEARelay"
_DISPLAY_NAME = "SA3 NMEA Relay"
_PLIST_LABEL = "com.sa3nmearelay"


# ---------------------------------------------------------------------------
# Icon
# ---------------------------------------------------------------------------

def _icon_path() -> Path:
    """Return path to heelappicon.png, works from source and PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return base / "heelappicon.png"


_BOAT_COLORS = {
    "connected": QColor("#27d17f"),
    "connecting": QColor("#ffc247"),
    "disconnected": QColor("#ff5b64"),
}
_CLIENT_COLORS = {
    False: QColor("#343a42"),
    True: None,  # Preserve the existing blue background when a client is connected.
}


def _make_icon(sailaway_state: str, has_clients: bool) -> QIcon:
    path = _icon_path()
    if path.exists():
        source = QImage(str(path)).scaled(
            128,
            128,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_ARGB32)
        icon = QImage(source)
        boat = _BOAT_COLORS.get(sailaway_state, _BOAT_COLORS["disconnected"])
        background = _CLIENT_COLORS[has_clients]

        for y in range(source.height()):
            for x in range(source.width()):
                pixel = source.pixelColor(x, y)
                # Remove the black matte outside the rounded tile. This is
                # transparent in tray icons so the menu bar shows through.
                if max(pixel.red(), pixel.green(), pixel.blue()) <= 35:
                    icon.setPixelColor(x, y, QColor(0, 0, 0, 0))
                    continue

                # The source icon uses a blue background and a white boat.
                if pixel.green() < 55 or pixel.blue() < 75:
                    continue
                alpha = max(0.0, min(1.0, (pixel.red() - 58.0) / 197.0))
                bg = background or pixel
                icon.setPixelColor(
                    x,
                    y,
                    QColor(
                        round(bg.red() * (1.0 - alpha) + boat.red() * alpha),
                        round(bg.green() * (1.0 - alpha) + boat.green() * alpha),
                        round(bg.blue() * (1.0 - alpha) + boat.blue() * alpha),
                        pixel.alpha(),
                    ),
                )
        return QIcon(QPixmap.fromImage(icon))
    # Fallback: simple coloured square
    px = QPixmap(22, 22)
    px.fill(Qt.GlobalColor.transparent)
    return QIcon(px)


# ---------------------------------------------------------------------------
# TrayManager
# ---------------------------------------------------------------------------

class TrayManager:
    """Creates and manages the system tray / menu bar icon."""

    def __init__(self, window):
        self._window = window
        self._tray: QSystemTrayIcon | None = None
        self._sailaway_state = "disconnected"
        self._client_count = 0

        # Load tray-only preference
        _s = QSettings(_APP_NAME, _APP_NAME)
        self._tray_only: bool = str(_s.value("app/trayOnlyMode", "false")).lower() in ("true", "1", "yes")

        if not QSystemTrayIcon.isSystemTrayAvailable():
            _logger.warning("System tray is not available on this platform.")
            return

        self._tray = QSystemTrayIcon()

        self._menu = QMenu()
        menu = self._menu

        self._act_sailaway_status = QAction(menu)
        self._act_sailaway_status.setEnabled(False)
        menu.addAction(self._act_sailaway_status)
        self._act_client_status = QAction(menu)
        self._act_client_status.setEnabled(False)
        menu.addAction(self._act_client_status)
        menu.addSeparator()

        # Show / Hide
        self._act_show = QAction(tr("tray_hide"))
        self._act_show.triggered.connect(self._on_show_hide)
        menu.addAction(self._act_show)

        menu.addSeparator()

        # Menu Bar / Tray Only
        self._act_tray_only = QAction(tr("tray_only_mode"))
        self._act_tray_only.setCheckable(True)
        self._act_tray_only.setChecked(self._tray_only)
        self._act_tray_only.triggered.connect(self._on_toggle_tray_only)
        menu.addAction(self._act_tray_only)

        # Start at Login
        self._act_login = QAction(tr("tray_start_at_login"))
        self._act_login.setCheckable(True)
        self._act_login.setChecked(self._is_start_at_login_enabled())
        self._act_login.triggered.connect(self._on_toggle_login)
        menu.addAction(self._act_login)

        menu.addSeparator()

        # Help
        self._act_help = QAction(tr("user_manual"))
        self._act_help.triggered.connect(self._on_help)
        menu.addAction(self._act_help)

        menu.addSeparator()

        # Quit
        self._act_quit = QAction(tr("tray_quit", app=_DISPLAY_NAME))
        self._act_quit.triggered.connect(QCoreApplication.quit)
        menu.addAction(self._act_quit)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)
        self.refresh_status()
        self._tray.show()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return self._tray is not None

    def is_tray_only(self) -> bool:
        return self._tray_only

    def set_sailaway_state(self, state: str) -> None:
        """Set the Sailaway link state: connected, connecting, or disconnected."""
        self._sailaway_state = state if state in _BOAT_COLORS else "disconnected"
        self.refresh_status()

    def set_client_count(self, count: int) -> None:
        """Set the number of connected relay clients."""
        self._client_count = max(0, int(count))
        self.refresh_status()

    def refresh_status(self) -> None:
        """Refresh the icon, tooltip, and menu summary from current states."""
        if not self._tray:
            return

        sailaway_text = tr(f"tray_sailaway_{self._sailaway_state}")
        client_text = tr("tray_client_count", n=self._client_count)
        self._tray.setIcon(_make_icon(self._sailaway_state, self._client_count > 0))
        self._tray.setToolTip(
            tr("tray_status_tooltip", sailaway=sailaway_text, clients=self._client_count)
        )
        self._act_sailaway_status.setText(tr("tray_sailaway_status", status=sailaway_text))
        self._act_client_status.setText(client_text)

    def refresh_language(self) -> None:
        """Refresh the tray's manual label after the app language changes."""
        if self._tray:
            self._act_help.setText(tr("user_manual"))

    def show_notification(self, title: str, msg: str) -> None:
        """Show a system notification if the tray is available."""
        if self._tray:
            self._tray.showMessage(
                title, msg,
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def on_window_shown(self):
        """Call when the main window becomes visible."""
        if self._tray:
            self._act_show.setText(tr("tray_hide"))

    def on_window_hidden(self):
        """Call when the main window is hidden to tray."""
        if self._tray:
            self._act_show.setText(tr("tray_show"))

    def on_window_visibility_changed(self, visible: bool):
        """Legacy helper — delegates to on_window_shown/on_window_hidden."""
        if visible:
            self.on_window_shown()
        else:
            self.on_window_hidden()

    # ------------------------------------------------------------------
    # Tray interaction
    # ------------------------------------------------------------------

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason):
        # Single click on Windows / double-click toggle
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_show_hide()

    def _on_show_hide(self):
        win = self._window
        if win.isVisible() and not win.isMinimized():
            win.hide()
        else:
            win.show()
            win.setWindowState(
                win.windowState() & ~Qt.WindowState.WindowMinimized
            )
            win.raise_()
            win.activateWindow()

    def _on_help(self):
        self._window.open_help()

    # ------------------------------------------------------------------
    # Start at Login
    # ------------------------------------------------------------------

    def _on_toggle_tray_only(self, checked: bool):
        self._tray_only = checked
        s = QSettings(_APP_NAME, _APP_NAME)
        s.setValue("app/trayOnlyMode", "true" if checked else "false")

    def _on_toggle_login(self, checked: bool):
        try:
            if checked:
                self._enable_login()
            else:
                self._disable_login()
        except Exception as exc:
            _logger.error("Failed to toggle Start at Login: %s", exc)
            # Revert the checkbox so the UI reflects actual state
            self._act_login.setChecked(not checked)

    def _is_start_at_login_enabled(self) -> bool:
        try:
            if platform.system() == "Darwin":
                return self._macos_plist_path().exists()
            if platform.system() == "Windows":
                return self._win_reg_exists()
        except Exception:
            pass
        return False

    def _enable_login(self):
        if platform.system() == "Darwin":
            self._macos_write_plist()
        elif platform.system() == "Windows":
            self._win_reg_write()

    def _disable_login(self):
        if platform.system() == "Darwin":
            p = self._macos_plist_path()
            if p.exists():
                p.unlink()
                _logger.info("LaunchAgent removed: %s", p)
        elif platform.system() == "Windows":
            self._win_reg_delete()

    # -- macOS LaunchAgent -------------------------------------------------

    def _macos_plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"

    def _macos_write_plist(self):
        if getattr(sys, "frozen", False):
            # PyInstaller .app bundle — point to the bundle executable
            program_args = f"        <string>{sys.executable}</string>"
        else:
            # Running from source
            python = sys.executable
            script = Path(__file__).resolve().parent / "main.py"
            program_args = (
                f"        <string>{python}</string>\n"
                f"        <string>{script}</string>"
            )

        log_file = Path.home() / "Library" / "Logs" / "SA3NMEARelay.log"
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"\n'
            '    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "    <key>Label</key>\n"
            f"    <string>{_PLIST_LABEL}</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            f"{program_args}\n"
            "    </array>\n"
            "    <key>RunAtLoad</key>\n"
            "    <true/>\n"
            "    <key>KeepAlive</key>\n"
            "    <false/>\n"
            "    <key>StandardOutPath</key>\n"
            f"    <string>{log_file}</string>\n"
            "    <key>StandardErrorPath</key>\n"
            f"    <string>{log_file}</string>\n"
            "</dict>\n"
            "</plist>\n"
        )

        path = self._macos_plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plist, encoding="utf-8")
        _logger.info("LaunchAgent written: %s", path)

    # -- Windows Registry --------------------------------------------------

    def _win_exe_cmd(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        python_dir = Path(sys.executable).parent
        pythonw = python_dir / "pythonw.exe"
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        script = Path(__file__).resolve().parent / "main.py"
        return f'"{pythonw}" "{script}"'

    def _win_reg_exists(self) -> bool:
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_QUERY_VALUE,
            )
            winreg.QueryValueEx(key, _APP_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            return False

    def _win_reg_write(self):
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, self._win_exe_cmd())
        winreg.CloseKey(key)
        _logger.info("Registry run key written for start at login.")

    def _win_reg_delete(self):
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            )
            winreg.DeleteValue(key, _APP_NAME)
            winreg.CloseKey(key)
            _logger.info("Registry run key removed.")
        except FileNotFoundError:
            pass
