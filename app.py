import json
import os
import queue
import sys
import textwrap
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, StringVar, Tk, ttk
from tkinter.scrolledtext import ScrolledText

import requests
import socketio

try:
    import win32print
except ImportError:  # pragma: no cover - handled at runtime for missing dependency
    win32print = None


APP_NAME = "Donation Receipt Printer"
STREAMLABS_SOCKET_TOKEN_URL = "https://streamlabs.com/api/v2.0/socket/token"
STREAMLABS_SOCKET_URL = "https://sockets.streamlabs.com"
RW80L_MKII_CHARS_PER_LINE = 48
RW80L_MKII_CODE_PAGE = 0  # PC437 in ESC/POS.
RW80L_MKII_FEED_LINES = 4
RW80L_MKII_PARTIAL_CUT_COMMAND = b"\x1d\x56\x01"


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

    text_payload = "\n".join(lines)
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
        chars_per_line: int = RW80L_MKII_CHARS_PER_LINE,
        code_page: int = RW80L_MKII_CODE_PAGE,
        feed_lines: int = RW80L_MKII_FEED_LINES,
        cut_command: bytes = RW80L_MKII_PARTIAL_CUT_COMMAND,
    ):
        self.printer_name = printer_name
        self.include_cut = include_cut
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
        try:
            socket_token = self._get_socket_token(access_token)
            self.log("Socket token acquired.")
        except Exception as exc:
            self.log(f"Failed to get socket token: {exc}")
            return

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
            self.log(f"Failed to connect to socket: {exc}")
            return

        while not self._stop_event.is_set():
            self._stop_event.wait(0.25)

        try:
            sio.disconnect()
        except Exception:
            pass

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
        self.root.geometry("820x580")

        self.log_queue = queue.Queue()
        self.listener = StreamlabsListener(self._queue_log, self._handle_donation)

        self.access_token_var = StringVar()
        self.printer_var = StringVar()
        self.cut_var = StringVar(value="yes")

        self._build_ui()
        self._load_config()
        self._refresh_printers()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_log_queue)

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="Streamlabs Access Token").grid(row=0, column=0, sticky="w")
        token_entry = ttk.Entry(frame, textvariable=self.access_token_var, width=90, show="*")
        token_entry.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(2, 10))

        ttk.Label(frame, text="Receipt Printer").grid(row=2, column=0, sticky="w")
        self.printer_combo = ttk.Combobox(frame, textvariable=self.printer_var, width=55, state="readonly")
        self.printer_combo.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 10))

        ttk.Button(frame, text="Refresh Printers", command=self._refresh_printers).grid(
            row=3, column=3, padx=(8, 0), sticky="ew"
        )
        ttk.Button(frame, text="Save Config", command=self._save_config).grid(
            row=3, column=4, padx=(8, 0), sticky="ew"
        )

        ttk.Label(frame, text="Auto-cut Receipt").grid(row=4, column=0, sticky="w")
        cut_combo = ttk.Combobox(frame, textvariable=self.cut_var, width=10, state="readonly")
        cut_combo["values"] = ("yes", "no")
        cut_combo.grid(row=5, column=0, sticky="w", pady=(2, 10))

        controls = ttk.Frame(frame)
        controls.grid(row=6, column=0, columnspan=5, sticky="ew", pady=(0, 10))
        ttk.Button(controls, text="Start Listening", command=self._start).pack(side=LEFT)
        ttk.Button(controls, text="Stop", command=self._stop).pack(side=LEFT, padx=(8, 0))
        ttk.Button(controls, text="Test Print", command=self._test_print).pack(side=LEFT, padx=(8, 0))
        ttk.Button(controls, text="Clear Log", command=self._clear_log).pack(side=RIGHT)

        self.log_text = ScrolledText(frame, height=22, state="normal")
        self.log_text.grid(row=7, column=0, columnspan=5, sticky="nsew")
        self.log_text.insert(END, f"{APP_NAME} ready.\n")
        self.log_text.configure(state="disabled")

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_columnconfigure(2, weight=1)
        frame.grid_columnconfigure(3, weight=0)
        frame.grid_columnconfigure(4, weight=0)
        frame.grid_rowconfigure(7, weight=1)

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

    def _save_config(self):
        config = {
            "access_token": self.access_token_var.get().strip(),
            "printer_name": self.printer_var.get().strip(),
            "cut_receipt": self.cut_var.get().strip().lower() == "yes",
        }
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
        self._queue_log(f"Saved config to {CONFIG_PATH}.")

    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.access_token_var.set(data.get("access_token", ""))
            self.printer_var.set(data.get("printer_name", ""))
            self.cut_var.set("yes" if data.get("cut_receipt", True) else "no")
            self._queue_log(f"Loaded config from {CONFIG_PATH}.")
        except Exception as exc:
            self._queue_log(f"Failed to load config: {exc}")

    def _build_printer_service(self) -> PrinterService:
        return PrinterService(
            printer_name=self.printer_var.get().strip(),
            include_cut=self.cut_var.get().strip().lower() == "yes",
        )

    def _start(self):
        access_token = self.access_token_var.get().strip()
        if not access_token:
            self._queue_log("Access token is required.")
            return
        if not self.printer_var.get().strip():
            self._queue_log("Select a printer before starting.")
            return
        try:
            self.listener.start(access_token)
            self._queue_log("Starting listener...")
        except Exception as exc:
            self._queue_log(f"Unable to start listener: {exc}")

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

    def _on_close(self):
        self.listener.stop()
        self.root.destroy()


def main():
    root = Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
