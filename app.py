"""Main application window for SA3 NMEA Relay.

Wires together:
  - ConnectionPanel (left dock)
  - NMEA sentence log (centre)
  - Status bar
  - TcpWorker (Sailaway inbound)
  - TCPRelay (outbound relay server)
  - NMEACorrector (sentence correction / filtering)
"""

from __future__ import annotations

import logging
import difflib
import time
from collections import deque
from typing import Optional

from PyQt6.QtCore import QCoreApplication, QSettings, QThread, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QPlainTextEdit,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from connection_panel import ConnectionPanel, _SETTINGS_ORG, _SETTINGS_APP
from nmea_corrector import correct_sentence
from tcp_client import TcpWorker
from tcp_relay import TCPRelay
from tray_manager import TrayManager
from translations import tr

_logger = logging.getLogger(__name__)

# Rolling log window: keep the last N seconds of sentences
_LOG_WINDOW_SECONDS = 30
_LOG_MAX_LINES = 500


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SA3 NMEA Relay")
        self.resize(1280, 820)
        self.setMinimumSize(1040, 700)

        # Runtime state
        self._tcp_worker: Optional[TcpWorker] = None
        self._tcp_thread: Optional[QThread] = None
        self._relay: Optional[TCPRelay] = None
        self._own_boat_id: Optional[str] = None  # detected from $SLBID
        self._sentence_count = 0
        self._relayed_count = 0
        self._log_lines: deque = deque(maxlen=_LOG_MAX_LINES)  # (timestamp, text)
        self._was_connected = False      # True once a session is established
        self._manual_disconnect = False  # True when user clicks Disconnect
        self._auto_retry_timer: Optional[QTimer] = None  # retries while Auto Connect is enabled

        self._build_ui()
        self._connect_signals()

        # System tray / menu bar icon
        self._tray = TrayManager(self)

        # Startup notification (delayed so tray icon is fully ready)
        QTimer.singleShot(800, self._show_startup_notification)

        # Status refresh timer
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status_bar)
        self._status_timer.start(1000)

        # Auto-connect on startup if configured. Keep this timer alive after a
        # successful connection so it can resume if Sailaway later disconnects.
        if self.conn_panel.is_auto_connect():
            QTimer.singleShot(400, self.conn_panel.btn_connect.click)
            self._ensure_auto_retry()

        # Auto-relay on startup if configured
        if self.conn_panel.is_auto_relay():
            QTimer.singleShot(600, self.conn_panel.tcp_relay_btn.click)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ---- Left panel: ConnectionPanel ----
        self.conn_panel = ConnectionPanel()
        self.conn_panel.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
        )

        # ---- Right panel: log + stats ----
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        # Stats bar
        stats_row = QHBoxLayout()
        self.lbl_rx = QLabel(tr("stat_received", n=0))
        self.lbl_relayed = QLabel(tr("stat_relayed", n=0))
        self.lbl_boat_id = QLabel(tr("stat_boat_id_none"))
        for lbl in (self.lbl_rx, self.lbl_relayed, self.lbl_boat_id):
            lbl.setObjectName("statBadge")
            stats_row.addWidget(lbl)
        stats_row.addStretch()
        right_layout.addLayout(stats_row)

        # Received and adjusted outgoing NMEA displays
        displays = QSplitter(Qt.Orientation.Vertical)
        displays.setChildrenCollapsible(False)
        displays.setHandleWidth(6)

        received_panel = QWidget()
        received_layout = QVBoxLayout(received_panel)
        received_layout.setContentsMargins(0, 0, 0, 0)
        received_layout.setSpacing(3)
        self.lbl_received_title = QLabel(tr("nmea_received_title"))
        self.lbl_received_title.setObjectName("nmeaHeading")
        received_layout.addWidget(self.lbl_received_title)

        self.log_viewer = QPlainTextEdit()
        self.log_viewer.setReadOnly(True)
        self.log_viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_viewer.setMaximumBlockCount(_LOG_MAX_LINES)
        self._style_nmea_view(self.log_viewer, "#f4f7fa")
        received_layout.addWidget(self.log_viewer)

        outgoing_panel = QWidget()
        outgoing_layout = QVBoxLayout(outgoing_panel)
        outgoing_layout.setContentsMargins(0, 0, 0, 0)
        outgoing_layout.setSpacing(3)
        self.lbl_outgoing_title = QLabel(tr("nmea_outgoing_title"))
        self.lbl_outgoing_title.setObjectName("nmeaHeading")
        outgoing_layout.addWidget(self.lbl_outgoing_title)

        self.outgoing_viewer = QTextEdit()
        self.outgoing_viewer.setReadOnly(True)
        self.outgoing_viewer.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.outgoing_viewer.document().setMaximumBlockCount(_LOG_MAX_LINES)
        self._style_nmea_view(self.outgoing_viewer, "#f4f7fa")
        outgoing_layout.addWidget(self.outgoing_viewer)

        displays.addWidget(received_panel)
        displays.addWidget(outgoing_panel)
        displays.setStretchFactor(0, 1)
        displays.setStretchFactor(1, 1)
        displays.setSizes([1, 1])
        right_layout.addWidget(displays)

        # Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.conn_panel)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 840])

        root.addWidget(splitter)

        # Status bar
        self.setStatusBar(QStatusBar())
        self.lbl_conn_status = QLabel(tr("status_disconnected"))
        self.lbl_conn_status.setStyleSheet("color: #fb7185;")
        self.statusBar().addWidget(self.lbl_conn_status)

        self.setStyleSheet(
            "QMainWindow { background-color: #202225; }"
            "QMenuBar { background: #303236; color: #f2f2f3; border-bottom: 1px solid #414348; }"
            "QMenuBar::item { padding: 6px 10px; background: transparent; }"
            "QMenuBar::item:selected { background: #4a4d52; border-radius: 7px; }"
            "QMenu { background: #303236; color: #f2f2f3; border: 1px solid #4a4d52; }"
            "QMenu::item { padding: 6px 24px; }"
            "QMenu::item:selected { background: #0a84ff; }"
            "QSplitter::handle { background-color: #3a3c40; }"
            "QSplitter::handle:horizontal { width: 5px; }"
            "QSplitter::handle:vertical { height: 5px; }"
            "QStatusBar { background-color: #292b2f; color: #d7d7d9; font-size: 9pt; border-top: 1px solid #3d3f43; }"
            "QLabel#statBadge { background: #303236; color: #e5e5e6; border: 1px solid #45474b; "
            "border-radius: 8px; padding: 4px 9px; font-size: 9pt; }"
            "QLabel#nmeaHeading { color: #f1f1f2; font-size: 10pt; font-weight: 600; "
            "padding: 5px 8px; background: #2b2d31; border: 1px solid #424448; border-radius: 7px; }"
        )

        # Menu bar – File menu
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu(tr("menu_file"))
        act_quit = file_menu.addAction(tr("menu_quit"))
        act_quit.triggered.connect(QCoreApplication.quit)

        # Menu bar – Help menu
        help_menu = menu_bar.addMenu(tr("menu_help"))
        act_manual = help_menu.addAction(tr("user_manual"))
        act_manual.triggered.connect(self.open_help)

    @staticmethod
    def _style_nmea_view(view, text_color: str):
        view.setStyleSheet(
            f"background-color: #111214; color: {text_color}; "
            "font-family: 'Menlo', 'Consolas', 'Courier New', monospace; "
            "font-size: 10pt; border: 1px solid #424448; border-radius: 7px; "
            "selection-background-color: #0a84ff; selection-color: #ffffff; padding: 6px;"
        )

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self.conn_panel.connect_clicked.connect(self._on_connect)
        self.conn_panel.disconnect_clicked.connect(self._on_disconnect)
        self.conn_panel.auto_connect_changed.connect(self._on_auto_connect_changed)
        self.conn_panel.tcp_relay_toggle.connect(self._on_relay_toggle)
        self.conn_panel.language_changed.connect(self._on_language_changed)
        self.conn_panel.user_manual_clicked.connect(self.open_help)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _on_connect(self, host: str, port: int):
        if self._tcp_worker is not None:
            return  # already connected

        self._tray.set_sailaway_state("connecting")

        if self.conn_panel.is_auto_connect():
            self._ensure_auto_retry()

        self._tcp_thread = QThread()
        self._tcp_worker = TcpWorker(host, port)
        self._tcp_worker.moveToThread(self._tcp_thread)

        self._tcp_thread.started.connect(self._tcp_worker.run)
        self._tcp_worker.connected.connect(lambda: self._on_connected(host, port))
        self._tcp_worker.disconnected.connect(self._on_disconnected)
        self._tcp_worker.error.connect(self._on_connect_error)
        self._tcp_worker.line.connect(self._on_nmea_line)

        self._tcp_thread.start()
        self.lbl_conn_status.setText(tr("status_connecting", host=host, port=port))
        self.lbl_conn_status.setStyleSheet("color: #fbbf24;")
        self.conn_panel.update_connection_status(False)

    def _auto_retry_connect(self):
        """Retry connect automatically while Auto Connect is enabled."""
        if not self.conn_panel.is_auto_connect():
            self._stop_auto_retry()
            return
        if self._tcp_worker is None and not self._manual_disconnect:
            self.conn_panel.btn_connect.click()

    def _ensure_auto_retry(self):
        if self._auto_retry_timer is None:
            self._auto_retry_timer = QTimer(self)
            self._auto_retry_timer.setInterval(10_000)
            self._auto_retry_timer.timeout.connect(self._auto_retry_connect)
        if not self._auto_retry_timer.isActive():
            self._auto_retry_timer.start()

    def _on_auto_connect_changed(self, enabled: bool):
        if enabled:
            self._ensure_auto_retry()
            if self._tcp_worker is None and not self._manual_disconnect:
                self._auto_retry_connect()
        else:
            self._stop_auto_retry()

    def _stop_auto_retry(self):
        if self._auto_retry_timer is not None:
            self._auto_retry_timer.stop()
            self._auto_retry_timer = None

    def _show_startup_notification(self):
        if self._tray.is_available():
            self._tray.show_notification(
                tr("notif_running_title"),
                tr("notif_running_msg"),
            )

    def _on_connected(self, host: str, port: int):
        self._was_connected = True
        self._manual_disconnect = False
        self._tray.set_sailaway_state("connected")
        self.lbl_conn_status.setText(tr("status_connected", host=host, port=port))
        self.lbl_conn_status.setStyleSheet("color: #34d399;")
        self.conn_panel.update_connection_status(True)
        self.conn_panel.record_success_host(host)
        self.conn_panel.record_success_port(port)
        self._log(tr("log_connected", host=host, port=port))
        if self._tray.is_available():
            self._tray.show_notification(
                tr("notif_connected_title"),
                tr("notif_connected_msg", host=host, port=port),
            )

    def _on_disconnect(self):
        self._manual_disconnect = True
        self._stop_auto_retry()  # user clicked Disconnect — cancel retries
        if self._tcp_worker:
            self._tcp_worker.stop()

    def _on_disconnected(self):
        was_connected = self._was_connected
        manual = self._manual_disconnect
        self._was_connected = False
        self._manual_disconnect = False

        self.lbl_conn_status.setText(tr("status_disconnected"))
        self.lbl_conn_status.setStyleSheet("color: #fb7185;")
        self.conn_panel.update_connection_status(False)
        if self._tcp_thread:
            self._tcp_thread.quit()
            self._tcp_thread.wait(2000)
        self._tcp_worker = None
        self._tcp_thread = None
        self._log(tr("log_disconnected"))

        auto_retrying = self.conn_panel.is_auto_connect() and not manual
        if auto_retrying:
            self._tray.set_sailaway_state("connecting")
            self._ensure_auto_retry()
            self.lbl_conn_status.setText(tr("status_reconnecting"))
            self.lbl_conn_status.setStyleSheet("color: #fbbf24;")
        else:
            self._tray.set_sailaway_state("disconnected")

        if was_connected and not manual:
            if not auto_retrying:
                self.lbl_conn_status.setText(tr("status_disconnected"))
            if self._tray.is_available():
                message = (
                    tr("notif_connection_lost_msg")
                    if auto_retrying
                    else tr("notif_connection_lost_manual_msg")
                )
                self._tray.show_notification(tr("notif_connection_lost_title"), message)

    def _on_connect_error(self, msg: str):
        self._tray.set_sailaway_state("disconnected")
        self.lbl_conn_status.setText(tr("status_error", msg=msg[:60]))
        self.lbl_conn_status.setStyleSheet("color: #fb7185;")
        self.conn_panel.update_connection_status(False)
        self._log(tr("log_error", msg=msg))

    # ------------------------------------------------------------------
    # NMEA sentence processing
    # ------------------------------------------------------------------

    def _on_nmea_line(self, text: str):
        self._sentence_count += 1

        # Detect Sailaway boat ID from $SLBID sentences
        stripped = text.strip()
        if stripped.startswith("$SLBID"):
            try:
                bid = stripped.split(",")[1].split("*")[0].strip()
                if bid and bid != self._own_boat_id:
                    self._own_boat_id = bid
                    self.lbl_boat_id.setText(tr("stat_boat_id", bid=bid))
                    self._log(tr("log_boat_id", bid=bid))
            except Exception:
                pass

        # Correct and optionally relay
        corrected = correct_sentence(
            stripped,
            self._own_boat_id,
            invert_heel=self.conn_panel.chk_invert_heel.isChecked(),
        )
        if corrected is not None:
            if self._relay and self._relay.running:
                self._relay.send_sentence(corrected, received=stripped)
                self._relayed_count += 1

        # Append to log viewer (show original with possible conversion note)
        self._log(stripped)

    def _append_outgoing(self, received: str, corrected: str | None):
        """Show the adjusted sentence, marking comma-separated fields that changed."""
        cursor = self.outgoing_viewer.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        document = self.outgoing_viewer.document()
        if not document.isEmpty():
            cursor.insertBlock()

        normal_format = QTextCharFormat()
        normal_format.setForeground(QColor("#f4f7fa"))
        changed_format = QTextCharFormat(normal_format)
        changed_format.setForeground(QColor("#ffffff"))
        changed_format.setBackground(QColor("#075fc4"))

        if corrected is None:
            filtered_format = QTextCharFormat(normal_format)
            filtered_format.setForeground(QColor("#ffffff"))
            cursor.insertText("[filtered] ", filtered_format)
            cursor.insertText(received, normal_format)
        else:
            outgoing = corrected.strip()
            before = received.split(",")
            after = outgoing.split(",")
            matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
            changed = set()
            for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
                if tag != "equal":
                    changed.update(range(j1, j2))

            for index, field in enumerate(after):
                cursor.insertText(field, changed_format if index in changed else normal_format)
                if index < len(after) - 1:
                    cursor.insertText(",", normal_format)

        # Keep the insertion point in the base style for the next live sentence.
        cursor.setCharFormat(normal_format)
        self.outgoing_viewer.setTextCursor(cursor)
        self.outgoing_viewer.ensureCursorVisible()
        self.outgoing_viewer.horizontalScrollBar().setValue(0)

    def _log(self, text: str):
        now = time.time()
        self._log_lines.append((now, text))
        # Trim old lines outside window
        cutoff = now - _LOG_WINDOW_SECONDS
        while self._log_lines and self._log_lines[0][0] < cutoff:
            self._log_lines.popleft()
        self.log_viewer.appendPlainText(text)
        self.log_viewer.horizontalScrollBar().setValue(0)

    def _refresh_status_bar(self):
        self.lbl_rx.setText(tr("stat_received", n=self._sentence_count))
        self.lbl_relayed.setText(tr("stat_relayed", n=self._relayed_count))

    def open_help(self):
        """Open the language-appropriate help file in the system default browser."""
        import os, sys
        from translations import get_language
        if getattr(sys, "frozen", False):
            base = sys._MEIPASS  # type: ignore[attr-defined]
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        lang = get_language()
        candidate = f"help_{lang}.html" if lang != "en" else "help.html"
        path = os.path.join(base, candidate)
        if not os.path.exists(path):
            path = os.path.join(base, "help.html")
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_language_changed(self, _lang: str):
        """Refresh any main-window strings that aren't in ConnectionPanel."""
        self.lbl_rx.setText(tr("stat_received", n=self._sentence_count))
        self.lbl_relayed.setText(tr("stat_relayed", n=self._relayed_count))
        self.lbl_received_title.setText(tr("nmea_received_title"))
        self.lbl_outgoing_title.setText(tr("nmea_outgoing_title"))
        self._tray.refresh_status()
        self._tray.refresh_language()
        if self._own_boat_id:
            self.lbl_boat_id.setText(tr("stat_boat_id", bid=self._own_boat_id))
        else:
            self.lbl_boat_id.setText(tr("stat_boat_id_none"))
        # Retranslate Help menu
        mb = self.menuBar()
        if mb.actions():
            mb.actions()[0].setText(tr("menu_help"))
            if mb.actions()[0].menu() and mb.actions()[0].menu().actions():
                mb.actions()[0].menu().actions()[0].setText(tr("user_manual"))

    # ------------------------------------------------------------------
    # TCP relay management
    # ------------------------------------------------------------------

    def _on_relay_toggle(self, host: str, port: int):
        # Use `is not None` — not `.running` — so the check is correct even
        # during the brief window after start() before the thread sets running=True.
        if self._relay is not None:
            self._relay.finished.disconnect(self._on_relay_thread_finished)
            self._relay.stop()
            self._relay = None
            self.conn_panel.update_tcp_relay_status(False, "Relay stopped")
            self.conn_panel.save_relay_settings()
            return

        self._relay = TCPRelay(host, port)
        self._relay.status_changed.connect(self.conn_panel.update_tcp_relay_status)
        self._relay.error_occurred.connect(self._on_relay_error)
        self._relay.client_count_changed.connect(self._on_relay_client_count_changed)
        self._relay.sentence_sent.connect(self._append_outgoing)
        # Track unexpected exits so self._relay is always cleared when the thread dies.
        self._relay.finished.connect(self._on_relay_thread_finished)
        self._relay.start()
        self.conn_panel.save_relay_settings()

    def _on_relay_thread_finished(self):
        """Called whenever the relay QThread exits (normally or due to an error).
        Clears self._relay so the toggle button is always in sync with reality.
        The TCPRelay._cleanup() already emits status_changed(False) which resets
        the button text, so we only need to clear the object reference here.
        """
        if self._relay is not None:
            self._relay = None
            self._on_relay_client_count_changed(0)

    def _on_relay_client_count_changed(self, count: int):
        self.conn_panel.update_tcp_client_count(count)
        self._tray.set_client_count(count)

    def _on_relay_error(self, msg: str):
        self.conn_panel.update_tcp_relay_error(msg)
        # Do NOT stop/clear here — the thread is already winding down and
        # _on_relay_thread_finished will clear self._relay when it finishes.

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        """Hide to tray instead of closing, if tray is available."""
        if self._tray.is_available():
            event.ignore()
            self.hide()
        else:
            self._do_cleanup()
            event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_tray") and self._tray.is_available():
            self._tray.on_window_shown()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "_tray") and self._tray.is_available():
            self._tray.on_window_hidden()

    def _do_cleanup(self):
        self._status_timer.stop()
        if self._tcp_worker:
            self._tcp_worker.stop()
        if self._relay:
            try:
                self._relay.finished.disconnect(self._on_relay_thread_finished)
            except Exception:
                pass
            self._relay.stop()
        if self._tcp_thread:
            self._tcp_thread.quit()
            self._tcp_thread.wait(2000)
