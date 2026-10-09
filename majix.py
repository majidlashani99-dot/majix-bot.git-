"""Windows screen scanner. No trades are placed. Run start.bat."""
import ctypes
import json
import math
import os
from pathlib import Path
import html
import io
import queue
import re
import shutil
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from datetime import datetime
from telegram_client import (DEFAULT_PROXY, MODES, TelegramError, normalize_proxy,
                             redact, send_photo, send_telegram, test_connection)

ROOT = Path(__file__).resolve().parent
DEFAULTS = {
    "token": "", "chat_id": "", "count": 40, "wait": 6.0,
    "signal": [990, 208], "sl": [990, 248], "tp1": [990, 269],
    "entry": [990, 228], "tp2": [990, 289], "levels": True,
    "photo": True, "shot": [56, 90, 1070, 668], "tesseract": "",
    "width": 1366, "height": 768, "cooldown": 1800.0,
    "popup": False, "sound": True, "symbols": "",
    "proxy": DEFAULT_PROXY, "mode": "auto",
}
MODE_LABELS = {"auto": "خودکار (پروکسی، بعد مستقیم/VPN)",
               "direct": "مستقیم / VPN (بدون پروکسی)",
               "proxy": "فقط پروکسی",
               "system": "پروکسی سیستم ویندوز"}
MODE_KEYS = {v: k for k, v in MODE_LABELS.items()}


def green(c):
    return c[1] > 100 and c[1] > c[0] + 25


def red(c):
    return c[0] > 130 and c[0] > c[1] + 30


def classify(sig, sl, tp):
    white = all(v > 210 for v in sig)
    if white or not (green(sig) or red(sig)):
        return None, "NO ACTIVE"
    if not red(sl):
        return None, "SL condition failed"
    if not green(tp):
        return None, "TP1 condition failed"
    return ("LONG" if green(sig) else "SHORT"), "ACTIVE"


TITLE_RE = re.compile(r"^\s*([A-Za-z0-9_.!:/-]+)\s+(\d[\d,]*(?:\.\d+)?)")


def parse_title(title):
    """TradingView tab title looks like 'BTCUSDT.P 84,919.8 ▲ +0.25% ...'."""
    m = TITLE_RE.match(title or "")
    return (m.group(1), m.group(2)) if m else (None, None)


PRICE_RE = re.compile(r"\d+\.\d+")
LEVEL_NAMES = (("entry", "ورود"), ("sl", "SL"), ("tp1", "TP1"), ("tp2", "TP2"))


def parse_price(text):
    m = PRICE_RE.search(text or "")
    return m.group(0) if m else None


def levels_ok(direction, levels):
    """SHORT: TP2 < TP1 < Entry < SL. LONG: the opposite."""
    try:
        e, sl, t1, t2 = (float(levels[k]) for k in ("entry", "sl", "tp1", "tp2"))
    except (TypeError, ValueError, KeyError):
        return False
    return (t2 < t1 < e < sl) if direction == "SHORT" else (sl < e < t1 < t2)


class OCRError(Exception):
    pass


def find_tesseract(cmd=""):
    candidates = [cmd] if cmd else []
    found = shutil.which("tesseract")
    if found:
        candidates.append(found)
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            sub = ("Programs", "Tesseract-OCR") if env == "LOCALAPPDATA" else ("Tesseract-OCR",)
            candidates.append(os.path.join(base, *sub, "tesseract.exe"))
    return next((p for p in candidates if p and os.path.isfile(p)), None)


def ocr_status(c):
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False, "کتابخانهٔ pytesseract نصب نیست؛ start.bat را دوباره اجرا کنید."
    path = find_tesseract(c.get("tesseract", ""))
    if not path:
        return False, ("برنامهٔ Tesseract روی این رایانه پیدا نشد؛ آن را نصب کنید "
                       "(winget install UB-Mannheim.TesseractOCR) یا مسیر tesseract.exe را در config.json بنویسید.")
    return True, path


def ocr_price(image, point, tesseract_cmd=""):
    """Read the first decimal number in the table cell whose right part is `point`."""
    try:
        import pytesseract
    except ImportError:
        raise OCRError("pytesseract نصب نیست") from None
    from PIL import Image, ImageChops, ImageOps
    path = find_tesseract(tesseract_cmd)
    if not path:
        raise OCRError("Tesseract نصب نیست")
    pytesseract.pytesseract.tesseract_cmd = path
    x, y = point
    crop = image.crop((x - 80, y - 8, x + 5, y + 9)).convert("RGB")
    crop = crop.resize((crop.width * 4, crop.height * 4), Image.LANCZOS)
    r, g, _ = crop.split()
    # Table text is white/yellow; backgrounds are red/green/dark -> keep only light R and G.
    mask = ImageChops.darker(r.point(lambda v: 255 if v > 130 else 0),
                             g.point(lambda v: 255 if v > 130 else 0))
    img = ImageOps.expand(ImageOps.invert(mask), border=12, fill=255)
    text = pytesseract.image_to_string(
        img, config="--psm 7 -c tessedit_char_whitelist=0123456789.()+-%")
    value = parse_price(text)
    if value is None:  # keep what the OCR saw, to diagnose a wrong coordinate
        try:
            folder = ROOT / "ocr_debug"
            folder.mkdir(exist_ok=True)
            img.save(folder / f"{x}_{y}.png")
        except OSError:
            pass
    return value


def pct(entry, value, sign):
    return f"{sign}{abs(float(value) - float(entry)) / float(entry) * 100:.2f}%"


def build_message(label, direction, price, levels, note, when):
    """Return (telegram_html, plain_text) for an active signal."""
    esc = html.escape
    long_ = direction == "LONG"
    lines = [f"{'🟢' if long_ else '🔴'} <b>سیگنال {'خرید' if long_ else 'فروش'} ({direction})</b>",
             f"💎 <b>{esc(str(label))}</b>", ""]
    if levels:
        entry = levels.get("entry")
        for emoji, name, key, sign in (("📍", "ورود", "entry", ""), ("🛑", "حد ضرر (SL)", "sl", "-"),
                                        ("🎯", "هدف اول (TP1)", "tp1", "+"), ("🎯", "هدف دوم (TP2)", "tp2", "+")):
            value = levels.get(key)
            if not value:
                lines.append(f"{emoji} {name}: نامشخص")
                continue
            line = f"{emoji} {name}: <code>{esc(value)}</code>"
            if sign and entry:
                try:
                    line += f"  ({pct(entry, value, sign)})"
                except (ValueError, ZeroDivisionError):
                    pass
            lines.append(line)
    if price:
        lines += ["", f"💵 قیمت لحظه‌ای: <code>{esc(price)}</code>"]
    if note:
        lines += ["", f"⚠️ {esc(note)}"]
    lines += ["", f"🕐 {when:%Y-%m-%d %H:%M:%S}"]
    text = "\n".join(lines)
    return text, html.unescape(re.sub(r"<[^>]+>", "", text))


def crop_box(shot, size):
    """Clamp the configured screenshot area to the real screen; whole screen if invalid."""
    width, height = size
    try:
        left, top, right, bottom = (int(v) for v in shot)
    except (TypeError, ValueError):
        return (0, 0, width, height)
    left, top, right, bottom = max(0, left), max(0, top), min(width, right), min(height, bottom)
    return (left, top, right, bottom) if right - left >= 50 and bottom - top >= 50 else (0, 0, width, height)


def average(image, point):
    x, y = point
    pixels = [image.getpixel((a, b))[:3]
              for a in range(x - 1, x + 2) for b in range(y - 1, y + 2)]
    return tuple(round(sum(p[i] for p in pixels) / 9) for i in range(3))


def validate(c, scope="all"):
    if scope in ("all", "telegram"):
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", c["token"]):
            raise ValueError("توکن بات را وارد کنید؛ قالب توکن معتبر نیست.")
        if not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", c["chat_id"]):
            raise ValueError("چت‌آیدی معتبر وارد کنید.")
        proxy = normalize_proxy(c.get("proxy", DEFAULT_PROXY))
        if c.get("mode", "auto") not in MODES:
            raise ValueError("حالت اتصال معتبر نیست.")
        if c.get("mode") == "proxy" and not proxy:
            raise ValueError("برای حالت «فقط پروکسی» آدرس پروکسی را وارد کنید.")
    if scope == "telegram":
        return
    if not 1 <= c["count"] <= 1000:
        raise ValueError("تعداد نماد باید بین ۱ و ۱۰۰۰ باشد.")
    for key in ("wait", "cooldown"):
        if not math.isfinite(c[key]) or c[key] < (1 if key == "wait" else 0):
            raise ValueError("زمان انتظار حداقل ۱ و فاصله هشدار حداقل صفر است.")
    if min(c["width"], c["height"]) < 3:
        raise ValueError("رزولوشن معتبر نیست.")
    for key in ("signal", "sl", "tp1", "entry", "tp2"):
        x, y = c[key]
        if not (1 <= x < c["width"] - 1 and 1 <= y < c["height"] - 1):
            raise ValueError("مختصات نمونه‌برداری خارج از صفحه است.")
    shot = c.get("shot", DEFAULTS["shot"])
    if (len(shot) != 4 or not 0 <= shot[0] < shot[2] <= c["width"] or not 0 <= shot[1] < shot[3] <= c["height"]
            or shot[2] - shot[0] < 50 or shot[3] - shot[1] < 50):
        raise ValueError("ناحیهٔ عکس باید چهار عدد x1,y1,x2,y2 داخل صفحه باشد.")
    names = [s.strip() for s in c["symbols"].split(",") if s.strip()]
    if names and len(names) != c["count"]:
        raise ValueError("نام‌ها را خالی بگذارید یا به تعداد نمادها، به ترتیب واچ‌لیست وارد کنید.")


class Dedup:
    def __init__(self, cooldown):
        self.sent = {}
        self.cooldown = cooldown

    def needed(self, idx, direction, now):
        if direction is None:
            self.sent.pop(idx, None)
            return False
        old = self.sent.get(idx)
        return old is None or old[0] != direction or now - old[1] >= self.cooldown

    def mark(self, idx, direction, now):
        self.sent[idx] = (direction, now)


class FocusLost(RuntimeError):
    pass


def while_focused(action, stop, emit):
    paused = False
    while not stop.is_set():
        try:
            value = action()
            if paused:
                emit("log", "پنجرهٔ اسکن دوباره فعال شد؛ ادامه از همان ردیف.")
            return value
        except FocusLost:
            if not paused:
                emit("log", "اسکن موقتاً مکث کرد؛ پنجرهٔ چارت را دوباره فعال کنید. کلیدی به پنجرهٔ دیگر ارسال نمی‌شود.")
            paused = True
            if stop.wait(0.3):
                return None
    return None


class Desktop:
    def __init__(self, c):
        import pyautogui
        from PIL import ImageGrab
        self.pg = pyautogui
        self.grab = ImageGrab.grab
        self.c = c
        self.user32 = ctypes.windll.user32
        self.user32.GetForegroundWindow.restype = ctypes.c_void_p
        self.user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        self.user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        self.user32.IsWindow.argtypes = [ctypes.c_void_p]
        self.hwnd = self.user32.GetForegroundWindow()
        process_id = ctypes.c_ulong()
        self.user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(process_id))
        if not self.hwnd or process_id.value == os.getpid():
            raise RuntimeError("پنجرهٔ چارت انتخاب نشده است. دوباره شروع کنید و در مهلت ۸ ثانیه روی واچ‌لیست کلیک کنید.")
        title = ctypes.create_unicode_buffer(1024)
        self.user32.GetWindowTextW(self.hwnd, title, 1024)
        self.title = title.value
        self.size = tuple(self.pg.size())
        self.check()

    def check(self):
        self.pg.failSafeCheck()
        if self.user32.GetAsyncKeyState(0x77) & 0x8000:  # F8
            raise RuntimeError("توقف اضطراری با F8.")
        if not self.user32.IsWindow(self.hwnd):
            raise RuntimeError("پنجرهٔ انتخاب‌شده بسته شده است؛ اسکن را دوباره شروع کنید.")
        if self.user32.GetForegroundWindow() != self.hwnd:
            raise FocusLost()
        width, height = self.pg.size()
        for key in ("signal", "sl", "tp1"):
            x, y = self.c[key]
            if not (1 <= x < width - 1 and 1 <= y < height - 1):
                raise RuntimeError(f"مختصات {key} خارج از صفحهٔ واقعی {width}×{height} است؛ از دکمهٔ انتخاب نقطه استفاده کنید.")

    def read_levels(self):
        self.check()
        with self.grab() as image:
            image.load()
            return {k: ocr_price(image, self.c[k], self.c.get("tesseract", ""))
                    for k, _ in LEVEL_NAMES}

    def screenshot(self):
        """JPEG bytes of the configured chart area."""
        self.check()
        with self.grab() as image:
            image.load()
            crop = image.crop(crop_box(self.c.get("shot"), image.size)).convert("RGB")
        buffer = io.BytesIO()
        crop.save(buffer, "JPEG", quality=85)
        return buffer.getvalue()

    def read_title(self):
        buf = ctypes.create_unicode_buffer(1024)
        self.user32.GetWindowTextW(self.hwnd, buf, 1024)
        return buf.value

    def sample(self):
        self.check()
        with self.grab() as image:
            values = [average(image, self.c[k]) for k in ("signal", "sl", "tp1")]
        self.check()
        return values

    def move(self, key, steps, stop):
        for _ in range(steps):
            if stop.is_set():
                return
            self.check()
            self.pg.press(key)
            if stop.wait(0.3):
                return


def scan(c, stop, emit, desktop_factory=Desktop, sender=send_telegram):
    try:
        if c.get("levels", True):
            ok, info = ocr_status(c)
            emit("log", ("OCR آماده است: " + info) if ok else ("هشدار OCR: " + info + " پیام‌ها بدون ورود/SL/TP ارسال می‌شوند."))
        emit("log", "۸ ثانیه: نماد اول واچ‌لیست را انتخاب و پنجرهٔ TradingView را فعال کنید.")
        if stop.wait(8):
            return
        desktop = desktop_factory(c)
        emit("log", "پنجرهٔ اسکن: " + getattr(desktop, "title", "پنجرهٔ انتخاب‌شده"))
        actual_size = getattr(desktop, "size", (c["width"], c["height"]))
        if actual_size != (c["width"], c["height"]):
            emit("log", f"توجه: اندازهٔ واقعی {actual_size} با تنظیمات فرق دارد؛ مختصات فعلی بدون تغییر استفاده می‌شود. تست رنگ را بررسی کنید.")
        names = [s.strip() for s in c["symbols"].split(",") if s.strip()]
        dedup = Dedup(c["cooldown"])
        local_dedup = Dedup(c["cooldown"])
        forward = True
        cycle = 1
        while not stop.is_set():
            emit("log", f"چرخه {cycle}؛ جهت {'پایین' if forward else 'بالا'}")
            indices = list(range(c["count"]))
            if not forward:
                indices.reverse()
            for position, idx in enumerate(indices):
                if stop.wait(c["wait"]):
                    return
                colors = while_focused(desktop.sample, stop, emit)
                if colors is None:
                    return
                direction, status = classify(*colors)
                symbol, price = parse_title(desktop.read_title() if hasattr(desktop, "read_title") else "")
                label = symbol or (names[idx] if names else f"ردیف #{idx + 1} واچ‌لیست")
                emit("log", f"{label}: {status} | RGB={colors}")
                emit("status", f"چرخه {cycle} | {label} | {direction or status}")
                local_alert = local_dedup.needed(idx, direction, time.monotonic())
                if dedup.needed(idx, direction, time.monotonic()):
                    levels, note = None, ""
                    if c.get("levels", True) and hasattr(desktop, "read_levels"):
                        try:
                            levels = while_focused(desktop.read_levels, stop, emit)
                            if levels is None:
                                return
                            emit("log", f"{label}: اعداد خوانده‌شده {levels}")
                            if not levels_ok(direction, levels):
                                note = "ترتیب اعداد منطقی نیست؛ خواندن OCR مشکوک است، روی چارت چک کنید."
                        except RuntimeError:
                            raise
                        except OCRError as exc:
                            emit("log", f"خواندن اعداد ناموفق: {exc}")
                            note = f"ورود/SL/TP خوانده نشد: {exc}."
                        except Exception as exc:
                            emit("log", redact(f"خواندن اعداد ناموفق: {type(exc).__name__}: {exc}", c))
                            note = "ورود/SL/TP خوانده نشد؛ جزئیات در majix.log است."
                    shot = None
                    if (c.get("photo", True) and sender is send_telegram and hasattr(desktop, "screenshot")):
                        try:
                            shot = while_focused(desktop.screenshot, stop, emit)
                            if shot is None:
                                return
                        except RuntimeError:
                            raise
                        except Exception as exc:
                            emit("log", redact(f"گرفتن عکس ناموفق: {type(exc).__name__}: {exc}", c))
                    html_text, text = build_message(label, direction, price, levels, note,
                                                    datetime.now().astimezone())
                    if local_alert:
                        local_dedup.mark(idx, direction, time.monotonic())
                        emit("log", f"سیگنال فعال تشخیص داده شد: {label} / {direction}")
                        if c["sound"]:
                            import winsound
                            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                        if c["popup"]:
                            emit("popup", text)
                    try:
                        emit("log", "در حال ارسال هشدار به تلگرام...")
                        if sender is not send_telegram:
                            sent = sender(c, text, stop)
                        elif shot and len(text) <= 900:
                            try:
                                sent = send_photo(c, shot, html_text, stop, emit=emit, parse_mode="HTML")
                            except TelegramError as exc:
                                if exc.code not in (400, 413):
                                    raise
                                emit("log", "تلگرام عکس را نپذیرفت؛ فقط متن ارسال می‌شود.")
                                sent = send_telegram(c, html_text, stop, emit=emit, parse_mode="HTML")
                        else:
                            sent = send_telegram(c, html_text, stop, emit=emit, parse_mode="HTML")
                        if sent:
                            dedup.mark(idx, direction, time.monotonic())
                            emit("log", f"ارسال شد: {label} / {direction}")
                    except TelegramError as exc:
                        emit("log", str(exc))
                if position < len(indices) - 1:
                    while_focused(lambda: desktop.move("down" if forward else "up", 1, stop), stop, emit)
            forward = not forward
            cycle += 1
            if stop.wait(5):
                return
    except Exception as exc:
        if type(exc).__name__ == "FailSafeException":
            reason = "توقف اضطراری: ماوس در گوشهٔ صفحه است. ماوس را جابه‌جا و دوباره شروع کنید."
        else:
            reason = redact(f"{type(exc).__name__}: {exc}", c)
        emit("log", reason)
        emit("error", reason)
    finally:
        emit("done", "")


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.testing = False
        self.popup_window = None
        root.title("MAJIX 2 | اسکنر TradingView → Telegram")
        root.geometry("880x800")
        root.minsize(800, 760)
        c = DEFAULTS.copy()
        try:
            c.update(json.loads((ROOT / "config.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.current = c.copy()
        self.vars = {}
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="MAJIX — اسکن رنگ صفحه و هشدار تلگرام", font=("Segoe UI", 16, "bold")).pack()
        notebook = ttk.Notebook(frame)
        notebook.pack(fill="x", pady=8)
        connection = ttk.Frame(notebook, padding=8)
        fields = ttk.Frame(notebook, padding=8)
        notebook.add(connection, text="تلگرام و پروکسی")
        notebook.add(fields, text="اسکن و مختصات")
        labels = [("token", "توکن بات"), ("chat_id", "Chat ID"), ("proxy", "پروکسی HTTP؛ خالی = مستقیم"),
                  ("count", "تعداد نماد"),
                  ("wait", "انتظار لود چارت (ثانیه)"), ("cooldown", "فاصله تکرار هشدار (ثانیه؛ صفر = هر بار)"),
                  ("width", "عرض صفحه"), ("height", "ارتفاع صفحه"),
                  ("signal", "مختصات سیگنال x,y"), ("sl", "مختصات SL x,y"),
                  ("tp1", "مختصات TP1 x,y"), ("entry", "مختصات ورود x,y"), ("tp2", "مختصات TP2 x,y"), ("shot", "ناحیهٔ عکس چارت x1,y1,x2,y2"), ("symbols", "نام نمادها به ترتیب، جدا با کاما (اختیاری)")]
        self.pick_buttons = []
        for row, (key, label) in enumerate(labels):
            parent = connection if row < 3 else fields
            row = row if row < 3 else row - 3
            value = ",".join(map(str, c[key])) if isinstance(c[key], list) else str(c[key])
            var = tk.StringVar(value=value)
            self.vars[key] = var
            ttk.Label(parent, text=label).grid(row=row, column=2, sticky="e", padx=8, pady=3)
            ttk.Entry(parent, textvariable=var, show="*" if key == "token" else "", width=40).grid(row=row, column=0, sticky="ew")
            if key in ("signal", "sl", "tp1", "entry", "tp2"):
                button = ttk.Button(parent, text="انتخاب نقطه", command=lambda k=key: self.pick(k))
                button.grid(row=row, column=1, padx=4)
                self.pick_buttons.append(button)
        self.vars["mode"] = tk.StringVar(value=MODE_LABELS.get(c.get("mode", "auto"), MODE_LABELS["auto"]))
        ttk.Label(connection, text="حالت اتصال").grid(row=3, column=2, sticky="e", padx=8, pady=3)
        ttk.Combobox(connection, textvariable=self.vars["mode"], values=list(MODE_LABELS.values()),
                     state="readonly", width=38).grid(row=3, column=0, sticky="ew")
        fields.columnconfigure(0, weight=1)
        connection.columnconfigure(0, weight=1)
        ttk.Label(connection, text="پروکسی شما: http://192.168.219.77:8080\nابتدا در گفت‌وگوی بات Start بزنید، سپس تست تلگرام را اجرا کنید.\nاگر VPN دارید «خودکار» یا «مستقیم / VPN» را انتخاب کنید؛ پروکسی لازم نیست.",
                  justify="right").grid(row=4, column=0, columnspan=3, sticky="e", pady=12)
        for key, label in (("sound", "صدای هشدار"), ("popup", "نمایش پنجره هشدار بدون توقف اسکن"),
                           ("levels", "خواندن ورود/SL/TP1/TP2 با OCR و ارسال در پیام"),
                           ("photo", "ارسال عکس چارت همراه پیام تلگرام")):
            self.vars[key] = tk.BooleanVar(value=c[key])
            ttk.Checkbutton(frame, text=label, variable=self.vars[key]).pack(anchor="e")
        ttk.Label(frame, text="توکن در config.json روی همین رایانه ذخیره می‌شود؛ فایل را به اشتراک نگذارید.").pack(pady=5)
        buttons = ttk.Frame(frame)
        buttons.pack(pady=8)
        self.start_button = ttk.Button(buttons, text="شروع اسکن", command=self.start)
        self.start_button.pack(side="right", padx=5)
        ttk.Button(buttons, text="توقف", command=self.request_stop).pack(side="right", padx=5)
        self.test_button = ttk.Button(buttons, text="تست تلگرام", command=self.test)
        self.test_button.pack(side="right", padx=5)
        self.sample_button = ttk.Button(buttons, text="تست رنگ صفحه", command=self.sample_screen)
        self.sample_button.pack(side="right", padx=5)
        ttk.Button(buttons, text="ذخیره تنظیمات", command=self.save).pack(side="right", padx=5)
        self.status = tk.StringVar(value="آماده؛ ابتدا تست تلگرام و تست رنگ صفحه را اجرا کنید.")
        ttk.Label(frame, textvariable=self.status, wraplength=820).pack(fill="x", pady=4)
        self.log = tk.Text(frame, height=10, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        self.emit("log", "نسخه ۲ — توقف اضطراری: F8 یا گوشهٔ صفحه. تغییر پنجره: مکث موقت اسکن.")
        root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def emit(self, kind, text):
        if isinstance(text, str):
            text = redact(text, self.current)
        self.events.put((kind, text))

    def request_stop(self):
        self.stop.set()
        self.status.set("درخواست توقف ثبت شد؛ اتصال در حال اجرا ممکن است تا پایان مهلت ۱۵ ثانیه طول بکشد.")

    def save(self, scope="all"):
        try:
            c = self.current.copy()
            for key, var in self.vars.items():
                is_connection = key in ("token", "chat_id", "proxy", "mode")
                if (scope == "telegram" and not is_connection) or (scope == "scan" and is_connection):
                    continue
                c[key] = var.get()
            if scope in ("all", "telegram"):
                for key in ("token", "chat_id"):
                    c[key] = c[key].strip()
                c["proxy"] = normalize_proxy(c["proxy"])
                c["mode"] = MODE_KEYS.get(c["mode"], c["mode"])
            if scope != "telegram":
                c["symbols"] = c["symbols"].strip()
                for key in ("count", "width", "height"):
                    c[key] = int(c[key])
                for key in ("wait", "cooldown"):
                    c[key] = float(c[key])
                for key in ("signal", "sl", "tp1", "entry", "tp2", "shot"):
                    c[key] = [int(x.strip()) for x in c[key].replace("،", ",").split(",")]
                    if len(c[key]) != (4 if key == "shot" else 2):
                        raise ValueError("ناحیهٔ عکس چهار عدد و مختصات دو عدد جداشده با کاما است.")
            validate(c, scope)
            temp = ROOT / "config.json.tmp"
            temp.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(ROOT / "config.json")
            self.current = c.copy()
            self.emit("log", "تنظیمات ذخیره شد؛ تغییرات برای شروع بعدی اعمال می‌شود.")
            return c
        except (ValueError, OSError) as exc:
            messagebox.showerror("تنظیمات", str(exc))
            return None

    def busy(self):
        return self.testing or (self.worker is not None and self.worker.is_alive())

    def set_busy(self, busy):
        for button in [self.start_button, self.test_button, self.sample_button, *self.pick_buttons]:
            button.configure(state="disabled" if busy else "normal")

    def start(self):
        if self.busy():
            return
        c = self.save()
        if c is None:
            return
        self.stop.clear()
        self.set_busy(True)
        self.status.set("۸ ثانیه فرصت: نماد اول واچ‌لیست را انتخاب کنید.")
        self.worker = threading.Thread(target=scan, args=(c, self.stop, self.emit), daemon=True)
        self.worker.start()
        self.root.iconify()

    def test(self):
        if self.busy():
            return
        c = self.save("telegram")
        if c is None:
            return
        self.stop.clear()
        self.testing = True
        self.set_busy(True)
        self.status.set("در حال تست اتصال تلگرام...")
        def run():
            try:
                success = test_connection(c, self.stop, self.emit)
                self.emit("result", "پیام تست با موفقیت به تلگرام ارسال شد." if success else "تست تلگرام لغو شد.")
            except Exception as exc:
                reason = redact(exc, c)
                self.emit("log", reason)
                self.emit("error", reason)
            finally:
                self.emit("tested", "")
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def sample_screen(self):
        if self.busy():
            return
        c = self.save("scan")
        if c is None:
            return
        self.stop.clear()
        self.testing = True
        self.set_busy(True)
        self.emit("log", "تست رنگ: ۸ ثانیه فرصت دارید پنجرهٔ چارت را فعال کنید. هیچ کلیدی یا پیام تلگرامی ارسال نمی‌شود.")
        def run():
            try:
                if self.stop.wait(8):
                    return
                desktop = Desktop(c)
                colors = desktop.sample()
                direction, status = classify(*colors)
                text = (f"تست رنگ: {direction or status}\n"
                        f"Signal RGB={colors[0]} | SL RGB={colors[1]} | TP1 RGB={colors[2]}\n"
                        f"صفحه: {desktop.size}؛ پنجره: {desktop.title}\n"
                        f"نماد/قیمت از عنوان: {parse_title(desktop.read_title())}")
                try:
                    (ROOT / "shot_test.jpg").write_bytes(desktop.screenshot())
                    text += f"\nعکس تست ذخیره شد: {ROOT / 'shot_test.jpg'} (آن را باز کنید و ناحیه را بررسی کنید)"
                except Exception as exc:
                    text += f"\nعکس تست ناموفق: {type(exc).__name__}: {exc}"
                try:
                    text += f"\nاعداد OCR: {desktop.read_levels()}"
                except Exception as exc:
                    text += f"\nاعداد OCR: ناموفق ({type(exc).__name__}: {exc})"
                self.emit("log", text)
                self.emit("result", text)
            except Exception as exc:
                reason = redact(exc, c)
                self.emit("log", reason)
                self.emit("error", reason)
            finally:
                self.emit("diagnosed", "")
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        self.root.iconify()

    def pick(self, key):
        if self.busy():
            return
        self.stop.clear()
        self.testing = True
        self.set_busy(True)
        self.emit("log", f"انتخاب {key}: تا ۵ ثانیه ماوس را روی بخش رنگی سلول نگه دارید؛ کلیک لازم نیست.")
        def run():
            try:
                if self.stop.wait(5):
                    return
                import pyautogui
                x, y = pyautogui.position()
                width, height = pyautogui.size()
                self.emit("point", (key, x, y, width, height))
            except Exception as exc:
                self.emit("error", redact(exc, self.current))
            finally:
                self.emit("diagnosed", "")
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        self.root.iconify()

    def poll(self):
        while not self.events.empty():
            kind, text = self.events.get_nowait()
            if kind == "log":
                line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n"
                try:
                    path = ROOT / "majix.log"
                    if path.exists() and path.stat().st_size > 1_000_000:
                        path.replace(ROOT / "majix.previous.log")
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(line)
                except OSError:
                    pass
                self.log.configure(state="normal")
                self.log.insert("end", line)
                if int(self.log.index("end-1c").split(".")[0]) > 500:
                    self.log.delete("1.0", "100.0")
                self.log.see("end")
                self.log.configure(state="disabled")
            elif kind in ("status", "result", "error"):
                self.status.set(text)
                if kind == "result":
                    self.emit("log", text)
                elif kind == "error":
                    self.root.deiconify()
            elif kind == "point":
                key, x, y, width, height = text
                self.vars[key].set(f"{x},{y}")
                self.vars["width"].set(str(width))
                self.vars["height"].set(str(height))
                self.status.set(f"نقطهٔ {key} انتخاب شد: ({x}, {y}). تنظیمات را ذخیره کنید.")
                self.emit("log", self.status.get())
            elif kind == "popup":
                if self.popup_window is not None and self.popup_window.winfo_exists():
                    self.popup_window.destroy()
                # Windows notification window shown without activating it.
                window = tk.Toplevel(self.root)
                self.popup_window = window
                window.withdraw()
                window.overrideredirect(True)
                window.attributes("-topmost", True)
                window.geometry("340x170+15+500")
                ttk.Label(window, text=text, wraplength=320, padding=10).pack(fill="both", expand=True)
                window.update_idletasks()
                user32 = ctypes.windll.user32
                user32.GetParent.argtypes = [ctypes.c_void_p]
                user32.GetParent.restype = ctypes.c_void_p
                user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
                hwnd = user32.GetParent(window.winfo_id())
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                window.after(5000, window.destroy)
            elif kind in ("done", "tested", "diagnosed"):
                self.testing = False
                self.set_busy(False)
                if kind in ("done", "diagnosed"):
                    self.root.deiconify()
                if kind == "done":
                    self.emit("log", "اسکن متوقف است. برای شروع مجدد، نماد اول را انتخاب کنید.")
        self.root.after(100, self.poll)

    def close(self):
        self.stop.set()
        self.root.destroy()


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("This scanner requires Windows.")
    ctypes.windll.user32.SetProcessDPIAware()
    App(tk.Tk()).root.mainloop()
