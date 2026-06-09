import json
import os
import queue
import secrets
import socket
import sys
import textwrap
import threading
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, StringVar, Tk, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import socketio

try:
    import win32con
    import win32print
    import win32ui
except ImportError:  # pragma: no cover - handled at runtime for missing dependency
    win32con = None
    win32print = None
    win32ui = None


APP_NAME = "Donation Receipt Printer"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:53177/callback"
STREAMLABS_SOCKET_TOKEN_URL = "https://streamlabs.com/api/v2.0/socket/token"
STREAMLABS_SOCKET_URL = "https://sockets.streamlabs.com"
STREAMLABS_AUTHORIZE_URL = "https://streamlabs.com/api/v2.0/authorize"
STREAMLABS_TOKEN_URL = "https://streamlabs.com/api/v2.0/token"
STREAMLABS_SCOPES = "socket.token donations.read"
RW80L_MKII_CHARS_PER_LINE = 48
RW80L_MKII_CODE_PAGE = 0  # PC437 in ESC/POS.
RW80L_MKII_FEED_LINES = 4
RW80L_MKII_PARTIAL_CUT_COMMAND = b"\x1d\x56\x01"
PRINT_MODE_AUTO = "auto"
PRINT_MODE_RAW = "raw"
PRINT_MODE_WINDOWS = "windows"

# PSX (Aerowinx Precision Simulator) EICAS integration constants.
PSX_DEFAULT_HOST = "localhost"
PSX_DEFAULT_PORT = 10747
PSX_FREEMSG_WARNING = 418  # Qs418 = FreeMsgW - custom Warning EICAS message (max 16 chars)
PSX_FREEMSG_CAUTION = 419  # Qs419 = FreeMsgC - custom Caution EICAS message (max 16 chars)
PSX_EICAS_MSG_UPD = 138    # Qi138 = EicasMsgUpd - triggers EICAS message list refresh
PSX_PRINTER_TEXT = 119     # Qs119 = PrinterText - ARINC 604 cockpit printer (max 24576 chars)
PSX_MAST_WARN_CP = 114     # Qh114 = MastWarnCp - Master Warning button Captain (BIGMOM)
PSX_MAST_WARN_FO = 115     # Qh115 = MastWarnFo - Master Warning button First Officer (BIGMOM)
PSX_EICAS_CANC = 111       # Qh111 = EicasCanc - EICAS Cancel button (DELTA)
PSX_BIGMOM_PUSHED = 1      # Bit 0: button pushed (DELTA)
PSX_BIGMOM_WARN_LIGHT = 128  # Bit 7: upper light contact = Warning (red)
PSX_BIGMOM_CAUT_LIGHT = 256  # Bit 8: lower light contact = Caution (amber)
PSX_DONATION_THRESHOLD = 50.0  # Donations above this trigger Warning; at or below trigger Caution
PSX_RECONNECT_INTERVAL = 10     # Seconds between PSX connection retries


class PSXClient:
    """TCP client for Aerowinx PSX network. Sends EICAS messages to the simulator."""

    def __init__(self, host: str = PSX_DEFAULT_HOST, port: int = PSX_DEFAULT_PORT, log=None):
        self.host = host
        self.port = port
        self.log = log or (lambda msg: None)
        self._socket = None
        self._lock = threading.Lock()
        self._connected = False
        self._reader_thread = None
        self._stop_event = threading.Event()
        self._has_donation_alert = False  # tracks if a donation EICAS message is active
        # Track current Master Warning button bitmask values from PSX
        self._mast_warn_cp_bits = 0
        self._mast_warn_fo_bits = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            try:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socket.settimeout(10)
                self._socket.connect((self.host, self.port))
                self._socket.settimeout(None)
                self._connected = True
                self._stop_event.clear()
                self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
                self._reader_thread.start()
                self.log(f"Connected to PSX at {self.host}:{self.port}")
            except Exception as exc:
                self._connected = False
                if self._socket:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                self._socket = None
                raise RuntimeError(f"Failed to connect to PSX at {self.host}:{self.port}: {exc}") from exc

    def connect_with_retry(self) -> None:
        """Keep trying to connect to PSX until successful or stopped."""
        while not self._stop_event.is_set():
            try:
                self.connect()
                return  # success
            except RuntimeError:
                self.log(f"PSX not available, retrying in {PSX_RECONNECT_INTERVAL}s...")
                self._stop_event.wait(PSX_RECONNECT_INTERVAL)

    def disconnect(self) -> None:
        with self._lock:
            self._stop_event.set()
            self._connected = False
            if self._socket:
                try:
                    self._send_raw("exit")
                except Exception:
                    pass
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None
            self.log("Disconnected from PSX.")

    def send_eicas_message(self, username: str, amount_str: str, message: str) -> None:
        """Send a donation notification to PSX EICAS using FreeMsgW/C variables
        and illuminate the Master Warning/Caution light to trigger the aural alert.

        >$50: FreeMsgW + Master Warning light (red) + fire bell
        <=$50: FreeMsgC + Master Caution light (amber) + single chime
        """
        if not self._connected:
            raise RuntimeError("Not connected to PSX.")

        amount_numeric = self._parse_amount(amount_str)

        if amount_numeric > PSX_DONATION_THRESHOLD:
            qs_index = PSX_FREEMSG_WARNING
            light_bit = PSX_BIGMOM_WARN_LIGHT
            level_label = "WARNING"
        else:
            qs_index = PSX_FREEMSG_CAUTION
            light_bit = PSX_BIGMOM_CAUT_LIGHT
            level_label = "CAUTION"

        eicas_text = "DONATION ALERT"

        # Set the EICAS free message text
        self._send_raw(f"Qs{qs_index}={eicas_text}")
        # Trigger EICAS message list refresh
        self._send_raw(f"Qi{PSX_EICAS_MSG_UPD}=1")

        # Illuminate the Master Warning/Caution light on the button to trigger
        # the aural alert (fire bell for Warning, chime for Caution).
        # Per PSX docs: "Network injectors can set any status directly."
        # We OR the light bit into the current bitmask to preserve other state.
        cp_bits = self._mast_warn_cp_bits | light_bit
        fo_bits = self._mast_warn_fo_bits | light_bit
        self._send_raw(f"Qh{PSX_MAST_WARN_CP}={cp_bits}")
        self._send_raw(f"Qh{PSX_MAST_WARN_FO}={fo_bits}")

        self._has_donation_alert = True
        self.log(f"PSX EICAS {level_label}: {eicas_text}")

    def clear_eicas_message(self) -> None:
        """Clear donation EICAS messages and extinguish the Master Warning/Caution light."""
        if not self._connected:
            return
        # Clear both free message slots
        self._send_raw(f"Qs{PSX_FREEMSG_WARNING}=")
        self._send_raw(f"Qs{PSX_FREEMSG_CAUTION}=")
        self._send_raw(f"Qi{PSX_EICAS_MSG_UPD}=1")

        # Clear the donation light bits from the Master Warning button
        # (preserve other bits like wired, bulb fails, etc.)
        clear_mask = ~(PSX_BIGMOM_WARN_LIGHT | PSX_BIGMOM_CAUT_LIGHT)
        cp_bits = self._mast_warn_cp_bits & clear_mask
        fo_bits = self._mast_warn_fo_bits & clear_mask
        self._send_raw(f"Qh{PSX_MAST_WARN_CP}={cp_bits}")
        self._send_raw(f"Qh{PSX_MAST_WARN_FO}={fo_bits}")

        self._has_donation_alert = False
        self.log("PSX EICAS donation alert cleared.")

    def send_printer_message(self, username: str, message: str, amount: str, currency: str) -> None:
        """Send a donation receipt to the PSX ARINC 604 cockpit printer (Qs119)."""
        if not self._connected:
            raise RuntimeError("Not connected to PSX.")

        donor = sanitize_text(username)[:30] or "Anonymous"
        msg = sanitize_text(message)[:200] or "(No message)"
        amt = sanitize_text(amount)[:15]
        cur = sanitize_text(currency)[:5]
        now = datetime.now().strftime("%d%b%y %H:%M")

        # Format as cockpit printer output — lines separated by newline chars
        # PSX PrinterText accepts plain text up to 24576 chars
        lines = [
            "=========================",
            "  DONATION RECEIVED",
            "=========================",
            f"TIME: {now}",
            f"FROM: {donor}",
        ]
        if amt:
            lines.append(f"AMT:  {amt} {cur}".strip())
        lines.append(f"MSG:  {msg}")
        lines.append("=========================")
        lines.append("")

        printer_text = "\n".join(lines)
        # Qs119 max is 24576 chars
        command = f"Qs{PSX_PRINTER_TEXT}={printer_text[:24576]}"
        self._send_raw(command)
        self.log(f"PSX Printer: receipt sent for {donor}")

    def _parse_amount(self, amount_str: str) -> float:
        """Extract numeric value from amount string like '$50.00' or '50.00 USD'."""
        cleaned = ""
        for ch in (amount_str or ""):
            if ch.isdigit() or ch == ".":
                cleaned += ch
        try:
            return float(cleaned) if cleaned else 0.0
        except ValueError:
            return 0.0

    def _send_raw(self, message: str) -> None:
        """Send a raw newline-terminated message to PSX."""
        if self._socket:
            try:
                self._socket.sendall((message + "\n").encode("ascii", errors="replace"))
            except Exception as exc:
                self._connected = False
                raise RuntimeError(f"PSX send failed: {exc}") from exc

    def _reader_loop(self) -> None:
        """Read incoming PSX messages. Tracks Master Warning bitmask state
        and watches for button presses to auto-clear donation alerts."""
        try:
            buf = b""
            while not self._stop_event.is_set():
                try:
                    if self._socket is None:
                        break
                    self._socket.settimeout(1.0)
                    data = self._socket.recv(4096)
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line_bytes, buf = buf.split(b"\n", 1)
                        self._process_incoming(line_bytes.decode("ascii", errors="replace").strip())
                except socket.timeout:
                    continue
                except Exception:
                    break
        finally:
            self._connected = False

    def _process_incoming(self, msg: str) -> None:
        """Process an incoming PSX message: track bitmask state and detect button presses."""
        if not msg.startswith("Q") or "=" not in msg:
            return
        try:
            eq = msg.index("=")
            q_code = msg[:eq]
            val_str = msg[eq + 1:].strip()

            if q_code[1] == "h":
                q_index = int(q_code[2:])
                val = int(val_str)

                # Track current Master Warning bitmask values
                if q_index == PSX_MAST_WARN_CP:
                    self._mast_warn_cp_bits = val
                elif q_index == PSX_MAST_WARN_FO:
                    self._mast_warn_fo_bits = val

                # Detect button presses to clear donation alert
                if self._has_donation_alert:
                    # Master Warning Captain or FO pressed (BIGMOM: bit 0 = pushed)
                    if q_index in (PSX_MAST_WARN_CP, PSX_MAST_WARN_FO) and (val & PSX_BIGMOM_PUSHED):
                        self.clear_eicas_message()
                    # EICAS Cancel button pressed (DELTA: non-zero = pressed)
                    elif q_index == PSX_EICAS_CANC and val != 0:
                        self.clear_eicas_message()
        except (ValueError, IndexError):
            pass


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()


def get_config_path() -> Path:
    # Installed apps usually run from Program Files, which is not user-writable.
    # Save config under per-user AppData so recipients can install and configure without admin edits.
    appdata = os.environ.get("APPDATA")
    if appdata:
        config_dir = Path(appdata) / "DonationReceiptPrinter"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "config.json"
    return APP_DIR / "config.json"


CONFIG_PATH = get_config_path()


def sanitize_text(value: str) -> str:
    text = (value or "").replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    return text.strip()


def wrap_text(value: str, width: int) -> list[str]:
    cleaned = sanitize_text(value)
    if not cleaned:
        return [""]
    return textwrap.wrap(cleaned, width=max(8, width), break_long_words=True, break_on_hyphens=False)


def add_labeled_lines(lines: list[str], label: str, value: str, width: int):
    prefix = f"{label}: "
    available = max(8, width - len(prefix))
    wrapped = textwrap.wrap(
        sanitize_text(value),
        width=available,
        break_long_words=True,
        break_on_hyphens=False,
    )
    if not wrapped:
        wrapped = [""]
    lines.append(prefix + wrapped[0])
    for line in wrapped[1:]:
        lines.append((" " * len(prefix)) + line)


def donation_receipt_lines(
    username: str,
    message: str,
    amount: str,
    currency: str,
    chars_per_line: int = RW80L_MKII_CHARS_PER_LINE,
) -> list[str]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    username = sanitize_text(username) or "Anonymous"
    message = sanitize_text(message) or "(No message)"
    amount = sanitize_text(amount)
    currency = sanitize_text(currency)
    chars_per_line = max(24, min(64, chars_per_line))
    separator = "-" * chars_per_line

    lines = [
        APP_NAME.center(chars_per_line),
        now.center(chars_per_line),
        separator,
    ]
    add_labeled_lines(lines, "From", username, chars_per_line)

    if amount:
        amount_value = f"{amount} {currency}".strip()
        add_labeled_lines(lines, "Amount", amount_value, chars_per_line)

    lines.append("")
    lines.append("Message:")
    lines.extend(wrap_text(message, chars_per_line))
    lines.extend(["", "Thank you!".center(chars_per_line), separator, ""])
    return lines


def escpos_receipt_bytes(
    username: str,
    message: str,
    amount: str,
    currency: str,
    include_cut: bool = True,
    chars_per_line: int = RW80L_MKII_CHARS_PER_LINE,
    code_page: int = RW80L_MKII_CODE_PAGE,
    feed_lines: int = RW80L_MKII_FEED_LINES,
    cut_command: bytes = RW80L_MKII_PARTIAL_CUT_COMMAND,
) -> bytes:
    text_payload = "\n".join(
        donation_receipt_lines(
            username=username,
            message=message,
            amount=amount,
            currency=currency,
            chars_per_line=chars_per_line,
        )
    )
    try:
        payload = text_payload.encode("cp437", errors="replace")
    except LookupError:
        payload = text_payload.encode("ascii", errors="replace")

    # RW80L MKII speaks ESC/POS. Set font A and default code page for predictable output.
    out = b"\x1b\x40" + b"\x1b\x4d\x00"
    if code_page is not None:
        out += b"\x1b\x74" + bytes([code_page & 0xFF])
    out += payload + (b"\n" * max(1, feed_lines))
    if include_cut:
        out += cut_command
    return out


class PrinterService:
    def __init__(
        self,
        printer_name: str,
        include_cut: bool = True,
        print_mode: str = PRINT_MODE_AUTO,
        chars_per_line: int = RW80L_MKII_CHARS_PER_LINE,
        code_page: int = RW80L_MKII_CODE_PAGE,
        feed_lines: int = RW80L_MKII_FEED_LINES,
        cut_command: bytes = RW80L_MKII_PARTIAL_CUT_COMMAND,
    ):
        self.printer_name = printer_name
        self.include_cut = include_cut
        self.print_mode = print_mode
        self.chars_per_line = chars_per_line
        self.code_page = code_page
        self.feed_lines = feed_lines
        self.cut_command = cut_command

    @staticmethod
    def list_printers() -> list[str]:
        if win32print is None:
            return []
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        printers = win32print.EnumPrinters(flags)
        names = []
        for printer in printers:
            # EnumPrinters returns tuples:
            # (flags, description, name, comment) for level 1
            names.append(printer[2])
        return sorted(set(names))

    def print_donation(self, username: str, message: str, amount: str, currency: str) -> None:
        if win32print is None:
            raise RuntimeError("pywin32 is not installed. Install dependencies first.")
        if not self.printer_name:
            raise RuntimeError("No printer selected.")

        resolved_mode = self._resolve_print_mode()
        if resolved_mode == PRINT_MODE_WINDOWS:
            self._print_donation_windows(username=username, message=message, amount=amount, currency=currency)
            return
        self._print_donation_raw(username=username, message=message, amount=amount, currency=currency)

    def _resolve_print_mode(self) -> str:
        mode = (self.print_mode or PRINT_MODE_AUTO).strip().lower()
        if mode in (PRINT_MODE_RAW, PRINT_MODE_WINDOWS):
            return mode

        name = (self.printer_name or "").lower()
        if "microsoft print to pdf" in name or name.endswith("pdf"):
            return PRINT_MODE_WINDOWS

        receipt_keywords = ("rw80", "receipt", "thermal", "esc/pos", "tm-", "xprinter", "pos")
        if any(keyword in name for keyword in receipt_keywords):
            return PRINT_MODE_RAW
        return PRINT_MODE_WINDOWS

    def _print_donation_raw(self, username: str, message: str, amount: str, currency: str) -> None:
        content = escpos_receipt_bytes(
            username=username,
            message=message,
            amount=amount,
            currency=currency,
            include_cut=self.include_cut,
            chars_per_line=self.chars_per_line,
            code_page=self.code_page,
            feed_lines=self.feed_lines,
            cut_command=self.cut_command,
        )
        handle = win32print.OpenPrinter(self.printer_name)
        try:
            job = win32print.StartDocPrinter(handle, 1, ("Donation Receipt", None, "RAW"))
            try:
                win32print.StartPagePrinter(handle)
                win32print.WritePrinter(handle, content)
                win32print.EndPagePrinter(handle)
            finally:
                win32print.EndDocPrinter(handle)
        finally:
            win32print.ClosePrinter(handle)

    def _print_donation_windows(self, username: str, message: str, amount: str, currency: str) -> None:
        if win32ui is None or win32con is None:
            raise RuntimeError("pywin32 UI modules are not installed. Reinstall dependencies.")

        lines = donation_receipt_lines(
            username=username,
            message=message,
            amount=amount,
            currency=currency,
            chars_per_line=self.chars_per_line,
        )

        dc = win32ui.CreateDC()
        dc.CreatePrinterDC(self.printer_name)
        font = None
        old_font = None
        started_doc = False
        started_page = False
        try:
            dc.StartDoc("Donation Receipt")
            started_doc = True
            dc.StartPage()
            started_page = True

            font = win32ui.CreateFont(
                {
                    "name": "Consolas",
                    "height": -28,
                    "weight": 400,
                }
            )
            old_font = dc.SelectObject(font)

            left_margin = 120
            top_margin = 120
            line_height = dc.GetTextExtent("Ag")[1] + 8
            max_y = dc.GetDeviceCaps(win32con.VERTRES) - top_margin
            y = top_margin

            for line in lines:
                if y + line_height > max_y:
                    dc.EndPage()
                    started_page = False
                    dc.StartPage()
                    started_page = True
                    if font is not None:
                        dc.SelectObject(font)
                    y = top_margin
                dc.TextOut(left_margin, y, line)
                y += line_height

            if started_page:
                dc.EndPage()
                started_page = False
            if started_doc:
                dc.EndDoc()
                started_doc = False
        finally:
            if old_font is not None:
                try:
                    dc.SelectObject(old_font)
                except Exception:
                    pass
            if font is not None:
                try:
                    font.DeleteObject()
                except Exception:
                    pass
            if started_page:
                try:
                    dc.EndPage()
                except Exception:
                    pass
            if started_doc:
                try:
                    dc.EndDoc()
                except Exception:
                    pass
            try:
                dc.DeleteDC()
            except Exception:
                pass


class StreamlabsListener:
    def __init__(self, log, on_donation):
        self.log = log
        self.on_donation = on_donation
        self._thread = None
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._sio = None
        self._seen_ids = deque()
        self._seen_set = set()
        self._seen_limit = 1000

    def start(self, access_token: str):
        if self.is_running():
            raise RuntimeError("Listener is already running.")
        self._stop_event.clear()
        self._connected_event.clear()
        self._thread = threading.Thread(target=self._run, args=(access_token,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._sio:
            try:
                self._sio.disconnect()
            except Exception:
                pass
        self._connected_event.clear()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _remember_event(self, event_key: str) -> bool:
        if event_key in self._seen_set:
            return False
        if len(self._seen_ids) >= self._seen_limit:
            stale = self._seen_ids.popleft()
            self._seen_set.discard(stale)
        self._seen_ids.append(event_key)
        self._seen_set.add(event_key)
        return True

    def _run(self, access_token: str):
        while not self._stop_event.is_set():
            try:
                socket_token = self._get_socket_token(access_token)
                self.log("Socket token acquired.")
            except Exception as exc:
                self.log(f"Failed to get socket token: {exc} — retrying in 15s...")
                self._stop_event.wait(15)
                continue

            sio = socketio.Client(
                reconnection=True,
                reconnection_attempts=0,
                reconnection_delay=1,
                reconnection_delay_max=10,
                logger=False,
                engineio_logger=False,
            )
            self._sio = sio

            @sio.event
            def connect():
                self._connected_event.set()
                self.log("Connected to Streamlabs socket.")

            @sio.event
            def disconnect():
                self._connected_event.clear()
                self.log("Disconnected from Streamlabs socket.")

            @sio.event
            def connect_error(data):
                self.log(f"Socket connection error: {data}")

            @sio.on("event")
            def on_event(event_data):
                try:
                    self._handle_event(event_data)
                except Exception as exc:
                    self.log(f"Failed to process event: {exc}")

            try:
                sio.connect(
                    f"{STREAMLABS_SOCKET_URL}?token={socket_token}",
                    transports=["websocket"],
                    wait_timeout=20,
                )
            except Exception as exc:
                self.log(f"Failed to connect to socket: {exc} — retrying in 15s...")
                self._stop_event.wait(15)
                continue

            while not self._stop_event.is_set():
                self._stop_event.wait(0.25)

            try:
                sio.disconnect()
            except Exception:
                pass
            break  # stop_event was set, exit the retry loop

    def _get_socket_token(self, access_token: str) -> str:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        response = requests.get(STREAMLABS_SOCKET_TOKEN_URL, headers=headers, timeout=20)
        if response.status_code >= 400:
            raise RuntimeError(f"{response.status_code}: {response.text}")
        payload = response.json()
        if isinstance(payload, dict):
            token = payload.get("socket_token") or payload.get("token")
            if token:
                return token
        raise RuntimeError(f"Unexpected response payload: {payload}")

    def _handle_event(self, event_data):
        if not isinstance(event_data, dict):
            return

        # Donation events from Streamlabs come as type=donation and include message[].
        if event_data.get("type") != "donation":
            return

        message_items = event_data.get("message")
        if not isinstance(message_items, list):
            return

        event_id = event_data.get("event_id")
        for item in message_items:
            if not isinstance(item, dict):
                continue

            donation_id = item.get("id") or item.get("_id")
            if donation_id:
                dedupe_key = f"{event_id}:{donation_id}"
            else:
                # Fallback key for payloads that omit donation IDs.
                dedupe_key = (
                    f"{event_id}:"
                    f"{item.get('name')}|{item.get('message')}|{item.get('amount')}|{item.get('formatted_amount')}"
                )
            if not self._remember_event(dedupe_key):
                continue

            username = item.get("name") or item.get("from") or "Anonymous"
            message = item.get("message") or ""
            amount = item.get("formatted_amount") or item.get("formattedAmount") or item.get("amount") or ""
            currency = item.get("currency") or ""
            self.on_donation(
                {
                    "username": str(username),
                    "message": str(message),
                    "amount": str(amount),
                    "currency": str(currency),
                }
            )


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("820x640")

        self.log_queue = queue.Queue()
        self.listener = StreamlabsListener(self._queue_log, self._handle_donation)

        self.client_id_var = StringVar()
        self.client_secret_var = StringVar()
        self.redirect_uri_var = StringVar(value=DEFAULT_REDIRECT_URI)
        self.access_token_var = StringVar()
        self.printer_var = StringVar()
        self.print_mode_var = StringVar(value=PRINT_MODE_AUTO)
        self.cut_var = StringVar(value="yes")
        self.refresh_token = ""
        self._oauth_thread = None

        # PSX EICAS integration (always on)
        self.psx_printer_var = StringVar(value="no")
        self.psx_host_var = StringVar(value=PSX_DEFAULT_HOST)
        self.psx_port_var = StringVar(value=str(PSX_DEFAULT_PORT))
        self.psx_client = None
        self._psx_connect_thread = None

        self._build_ui()
        self._load_config()
        self._refresh_printers()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_log_queue)
        # Auto-start PSX connection and Streamlabs listener
        self.root.after(500, self._auto_start)

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="Streamlabs Client ID").grid(row=0, column=0, sticky="w")
        client_id_entry = ttk.Entry(frame, textvariable=self.client_id_var, width=90)
        client_id_entry.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(2, 10))

        ttk.Label(frame, text="Streamlabs Client Secret").grid(row=2, column=0, sticky="w")
        client_secret_entry = ttk.Entry(frame, textvariable=self.client_secret_var, width=90, show="*")
        client_secret_entry.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(2, 10))
        ttk.Button(frame, text="Connect Streamlabs", command=self._connect_streamlabs).grid(
            row=3, column=4, padx=(8, 0), sticky="ew"
        )

        ttk.Label(frame, text="Redirect URI (must match your Streamlabs app)").grid(row=4, column=0, sticky="w")
        redirect_entry = ttk.Entry(frame, textvariable=self.redirect_uri_var, width=90)
        redirect_entry.grid(row=5, column=0, columnspan=5, sticky="ew", pady=(2, 10))

        ttk.Label(frame, text="Streamlabs Access Token").grid(row=6, column=0, sticky="w")
        token_entry = ttk.Entry(frame, textvariable=self.access_token_var, width=90, show="*")
        token_entry.grid(row=7, column=0, columnspan=5, sticky="ew", pady=(2, 10))

        ttk.Label(frame, text="Receipt Printer").grid(row=8, column=0, sticky="w")
        self.printer_combo = ttk.Combobox(frame, textvariable=self.printer_var, width=55, state="readonly")
        self.printer_combo.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(2, 10))

        ttk.Button(frame, text="Refresh Printers", command=self._refresh_printers).grid(
            row=9, column=3, padx=(8, 0), sticky="ew"
        )
        ttk.Button(frame, text="Save Config", command=self._save_config).grid(
            row=9, column=4, padx=(8, 0), sticky="ew"
        )

        ttk.Label(frame, text="Auto-cut Receipt").grid(row=10, column=0, sticky="w")
        ttk.Label(frame, text="Print Mode").grid(row=10, column=1, sticky="w")
        cut_combo = ttk.Combobox(frame, textvariable=self.cut_var, width=10, state="readonly")
        cut_combo["values"] = ("yes", "no")
        cut_combo.grid(row=11, column=0, sticky="w", pady=(2, 10))
        print_mode_combo = ttk.Combobox(frame, textvariable=self.print_mode_var, width=12, state="readonly")
        print_mode_combo["values"] = (PRINT_MODE_AUTO, PRINT_MODE_RAW, PRINT_MODE_WINDOWS)
        print_mode_combo.grid(row=11, column=1, sticky="w", pady=(2, 10))

        # PSX Integration
        ttk.Label(frame, text="PSX Printer").grid(row=12, column=0, sticky="w")
        ttk.Label(frame, text="PSX Host").grid(row=12, column=1, sticky="w")
        ttk.Label(frame, text="PSX Port").grid(row=12, column=2, sticky="w")
        psx_printer_combo = ttk.Combobox(frame, textvariable=self.psx_printer_var, width=10, state="readonly")
        psx_printer_combo["values"] = ("yes", "no")
        psx_printer_combo.grid(row=13, column=0, sticky="w", pady=(2, 10))
        ttk.Entry(frame, textvariable=self.psx_host_var, width=18).grid(row=13, column=1, sticky="w", pady=(2, 10))
        ttk.Entry(frame, textvariable=self.psx_port_var, width=8).grid(row=13, column=2, sticky="w", pady=(2, 10))
        ttk.Button(frame, text="Test PSX", command=self._test_psx).grid(
            row=13, column=3, padx=(8, 0), sticky="ew"
        )

        controls = ttk.Frame(frame)
        controls.grid(row=14, column=0, columnspan=5, sticky="ew", pady=(0, 10))
        ttk.Button(controls, text="Start Listening", command=self._start).pack(side=LEFT)
        ttk.Button(controls, text="Stop", command=self._stop).pack(side=LEFT, padx=(8, 0))
        ttk.Button(controls, text="Test Print", command=self._test_print).pack(side=LEFT, padx=(8, 0))
        ttk.Button(controls, text="Clear Log", command=self._clear_log).pack(side=RIGHT)

        self.log_text = ScrolledText(frame, height=18, state="normal")
        self.log_text.grid(row=15, column=0, columnspan=5, sticky="nsew")
        self.log_text.insert(END, f"{APP_NAME} ready.\n")
        self.log_text.configure(state="disabled")

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_columnconfigure(2, weight=1)
        frame.grid_columnconfigure(3, weight=0)
        frame.grid_columnconfigure(4, weight=0)
        frame.grid_rowconfigure(15, weight=1)

    def _queue_log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _drain_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert(END, message + "\n")
                self.log_text.see(END)
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", END)
        self.log_text.configure(state="disabled")

    def _refresh_printers(self):
        printers = PrinterService.list_printers()
        self.printer_combo["values"] = printers
        if printers and self.printer_var.get() not in printers:
            self.printer_var.set(printers[0])
        if not printers:
            self._queue_log("No printers found. Make sure your printer driver is installed.")
        else:
            self._queue_log(f"Loaded {len(printers)} printer(s).")

    def _connect_streamlabs(self):
        if self._oauth_thread is not None and self._oauth_thread.is_alive():
            self._queue_log("Streamlabs OAuth is already in progress.")
            return

        client_id = self.client_id_var.get().strip()
        client_secret = self.client_secret_var.get().strip()
        redirect_uri = self.redirect_uri_var.get().strip()
        if not client_id or not client_secret:
            self._queue_log("Client ID and Client Secret are required.")
            return
        if not redirect_uri:
            self._queue_log("Redirect URI is required.")
            return

        self._oauth_thread = threading.Thread(
            target=self._run_connect_flow,
            args=(client_id, client_secret, redirect_uri),
            daemon=True,
        )
        self._oauth_thread.start()

    def _run_connect_flow(self, client_id: str, client_secret: str, redirect_uri: str):
        try:
            state = secrets.token_urlsafe(18)
            code = self._wait_for_authorization_code(client_id, redirect_uri, state)
            token_payload = self._exchange_authorization_code(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                code=code,
            )
            access_token = token_payload.get("access_token")
            if not access_token:
                raise RuntimeError(f"Token response missing access_token: {token_payload}")

            refresh_token = token_payload.get("refresh_token") or ""
            expires_in = token_payload.get("expires_in")

            self.refresh_token = str(refresh_token)
            self.root.after(0, lambda: self._apply_connected_token(str(access_token)))
            self._queue_log("Streamlabs connected. Access token populated.")
            if expires_in:
                self._queue_log(f"Access token expires in {expires_in} seconds.")
        except Exception as exc:
            self._queue_log(f"Streamlabs OAuth failed: {exc}")

    def _apply_connected_token(self, access_token: str):
        self.access_token_var.set(access_token)
        self._save_config()

    def _build_authorize_url(self, client_id: str, redirect_uri: str, state: str) -> str:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": STREAMLABS_SCOPES,
            "state": state,
        }
        return f"{STREAMLABS_AUTHORIZE_URL}?{urlencode(params)}"

    def _wait_for_authorization_code(self, client_id: str, redirect_uri: str, expected_state: str) -> str:
        parsed = urlparse(redirect_uri)
        if parsed.scheme.lower() != "http":
            raise RuntimeError("Redirect URI must use http:// for local callback.")
        if parsed.hostname not in ("127.0.0.1", "localhost"):
            raise RuntimeError("Redirect URI host must be 127.0.0.1 or localhost.")
        if parsed.port is None:
            raise RuntimeError("Redirect URI must include an explicit port.")

        callback_path = parsed.path or "/"
        callback_result = {"code": None, "error": None}
        callback_event = threading.Event()
        app = self

        class OAuthHandler(BaseHTTPRequestHandler):
            def _send_html(self, status_code: int, body: str):
                payload = body.encode("utf-8", errors="replace")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                request_url = urlparse(self.path)
                if request_url.path != callback_path:
                    self._send_html(
                        404,
                        "<html><body><h2>Not found</h2></body></html>",
                    )
                    return

                query = parse_qs(request_url.query, keep_blank_values=True)
                received_state = (query.get("state") or [None])[0]
                error = (query.get("error") or [None])[0]
                code = (query.get("code") or [None])[0]

                if received_state != expected_state:
                    callback_result["error"] = "State verification failed."
                    self._send_html(
                        400,
                        "<html><body><h2>Authorization failed</h2><p>State mismatch.</p></body></html>",
                    )
                    callback_event.set()
                    return

                if error:
                    callback_result["error"] = f"Provider returned error: {error}"
                    self._send_html(
                        400,
                        "<html><body><h2>Authorization denied</h2><p>You can close this window.</p></body></html>",
                    )
                    callback_event.set()
                    return

                if not code:
                    callback_result["error"] = "Missing authorization code."
                    self._send_html(
                        400,
                        "<html><body><h2>Authorization failed</h2><p>Missing code.</p></body></html>",
                    )
                    callback_event.set()
                    return

                callback_result["code"] = code
                self._send_html(
                    200,
                    "<html><body><h2>Connected</h2><p>You can close this window and return to the app.</p></body></html>",
                )
                callback_event.set()

            def log_message(self, format, *args):  # noqa: A003
                # Suppress noisy callback request logs from stderr.
                return

        try:
            server = HTTPServer((parsed.hostname, parsed.port), OAuthHandler)
        except OSError as exc:
            raise RuntimeError(f"Could not start callback server on {parsed.hostname}:{parsed.port}: {exc}") from exc

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        authorize_url = self._build_authorize_url(client_id, redirect_uri, expected_state)
        app._queue_log("Opening browser for Streamlabs authorization...")
        browser_opened = webbrowser.open(authorize_url, new=1, autoraise=True)
        if not browser_opened:
            app._queue_log(f"Open this URL manually to continue OAuth: {authorize_url}")

        callback_event.wait(timeout=240)
        server.shutdown()
        server.server_close()

        if not callback_event.is_set():
            raise RuntimeError("Authorization timed out. Try Connect Streamlabs again.")
        if callback_result["error"]:
            raise RuntimeError(callback_result["error"])
        return str(callback_result["code"])

    def _exchange_authorization_code(
        self, client_id: str, client_secret: str, redirect_uri: str, code: str
    ) -> dict:
        form_data = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        response = requests.post(
            STREAMLABS_TOKEN_URL,
            data=form_data,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=20,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{response.status_code}: {response.text}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected token response: {payload}")
        return payload

    def _save_config(self):
        config = {
            "client_id": self.client_id_var.get().strip(),
            "client_secret": self.client_secret_var.get().strip(),
            "redirect_uri": self.redirect_uri_var.get().strip(),
            "access_token": self.access_token_var.get().strip(),
            "refresh_token": self.refresh_token,
            "printer_name": self.printer_var.get().strip(),
            "print_mode": self.print_mode_var.get().strip().lower() or PRINT_MODE_AUTO,
            "cut_receipt": self.cut_var.get().strip().lower() == "yes",
            "psx_printer": self.psx_printer_var.get().strip().lower() == "yes",
            "psx_host": self.psx_host_var.get().strip() or PSX_DEFAULT_HOST,
            "psx_port": self.psx_port_var.get().strip() or str(PSX_DEFAULT_PORT),
        }
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self._queue_log(f"Saved config to {CONFIG_PATH}.")

    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.client_id_var.set(data.get("client_id", ""))
            self.client_secret_var.set(data.get("client_secret", ""))
            self.redirect_uri_var.set(data.get("redirect_uri", DEFAULT_REDIRECT_URI))
            self.access_token_var.set(data.get("access_token", ""))
            self.refresh_token = data.get("refresh_token", "")
            self.printer_var.set(data.get("printer_name", ""))
            configured_mode = str(data.get("print_mode", PRINT_MODE_AUTO)).strip().lower()
            if configured_mode not in (PRINT_MODE_AUTO, PRINT_MODE_RAW, PRINT_MODE_WINDOWS):
                configured_mode = PRINT_MODE_AUTO
            self.print_mode_var.set(configured_mode)
            self.cut_var.set("yes" if data.get("cut_receipt", True) else "no")
            self.psx_printer_var.set("yes" if data.get("psx_printer", False) else "no")
            self.psx_host_var.set(data.get("psx_host", PSX_DEFAULT_HOST))
            self.psx_port_var.set(data.get("psx_port", str(PSX_DEFAULT_PORT)))
            self._queue_log(f"Loaded config from {CONFIG_PATH}.")
        except Exception as exc:
            self._queue_log(f"Failed to load config: {exc}")

    def _build_printer_service(self) -> PrinterService:
        return PrinterService(
            printer_name=self.printer_var.get().strip(),
            include_cut=self.cut_var.get().strip().lower() == "yes",
            print_mode=self.print_mode_var.get().strip().lower() or PRINT_MODE_AUTO,
        )

    def _start(self):
        access_token = self.access_token_var.get().strip()
        if not access_token:
            self._queue_log("Access token is required.")
            return
        try:
            self.listener.start(access_token)
            self._queue_log("Starting listener...")
        except Exception as exc:
            self._queue_log(f"Unable to start listener: {exc}")

    def _auto_start(self):
        """Auto-start PSX connection and Streamlabs listener on app launch."""
        # Start PSX connection with retry in background
        self._start_psx_background()
        # Auto-start Streamlabs listener if access token is available
        access_token = self.access_token_var.get().strip()
        if access_token:
            try:
                self.listener.start(access_token)
                self._queue_log("Auto-starting Streamlabs listener...")
            except Exception as exc:
                self._queue_log(f"Auto-start listener failed: {exc}")
        else:
            self._queue_log("No access token saved — connect Streamlabs to begin.")

    def _start_psx_background(self):
        """Start PSX connection in a background thread with auto-retry."""
        if self._psx_connect_thread and self._psx_connect_thread.is_alive():
            return
        host = self.psx_host_var.get().strip() or PSX_DEFAULT_HOST
        try:
            port = int(self.psx_port_var.get().strip())
        except ValueError:
            port = PSX_DEFAULT_PORT
        self.psx_client = PSXClient(host=host, port=port, log=self._queue_log)
        self._psx_connect_thread = threading.Thread(
            target=self.psx_client.connect_with_retry, daemon=True
        )
        self._psx_connect_thread.start()
        self._queue_log(f"PSX EICAS connecting to {host}:{port}...")

    def _stop(self):
        self.listener.stop()
        self._queue_log("Stopping listener...")

    def _test_print(self):
        printer = self._build_printer_service()
        try:
            printer.print_donation(
                username="TestUser",
                message="This is a Streamlabs print test.",
                amount="$1.00",
                currency="USD",
            )
            self._queue_log("Test print sent.")
        except Exception as exc:
            self._queue_log(f"Test print failed: {exc}")

    def _handle_donation(self, donation: dict):
        username = donation["username"]
        message = donation["message"]
        amount = donation["amount"]
        currency = donation["currency"]
        self._queue_log(f"Donation received from {username}: {message}")

        printer = self._build_printer_service()
        try:
            printer.print_donation(username=username, message=message, amount=amount, currency=currency)
            self._queue_log(f"Printed donation from {username}.")
        except Exception as exc:
            self._queue_log(f"Print failed for {username}: {exc}")

        # Send to PSX if enabled (EICAS alert and/or ARINC printer)
        self._send_to_psx(username=username, amount=amount, message=message, currency=currency)

    def _get_psx_client(self) -> PSXClient | None:
        """Get the PSX client if connected."""
        if self.psx_client and self.psx_client.connected:
            return self.psx_client
        return None

    def _send_to_psx(self, username: str, amount: str, message: str, currency: str = "") -> None:
        """Send donation to PSX EICAS and optionally ARINC printer."""
        try:
            client = self._get_psx_client()
            if not client:
                self._queue_log("PSX not connected — EICAS alert skipped.")
                return
            client.send_eicas_message(username=username, amount_str=amount, message=message)
            if self.psx_printer_var.get().strip().lower() == "yes":
                client.send_printer_message(username=username, message=message, amount=amount, currency=currency)
        except Exception as exc:
            self._queue_log(f"PSX failed: {exc}")

    def _test_psx(self):
        """Test PSX connection by sending a test donation to EICAS and/or printer."""
        try:
            client = self._get_psx_client()
            if not client:
                self._queue_log("PSX not connected yet — waiting for connection.")
                return
            client.send_eicas_message(
                username="TestDonor",
                amount_str="$25.00",
                message="PSX EICAS test message",
            )
            self._queue_log("PSX EICAS test sent (Caution level - $25 test).")
            if self.psx_printer_var.get().strip().lower() == "yes":
                client.send_printer_message(
                    username="TestDonor",
                    message="This is a PSX printer test",
                    amount="$25.00",
                    currency="USD",
                )
                self._queue_log("PSX Printer test receipt sent.")
        except Exception as exc:
            self._queue_log(f"PSX test failed: {exc}")

    def _on_close(self):
        self.listener.stop()
        if self.psx_client:
            try:
                self.psx_client.disconnect()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
