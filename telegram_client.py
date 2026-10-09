"""Telegram transport: HTTP proxy, direct (VPN) or automatic fallback; redacted errors."""
import json
import re
import ssl
import uuid
from urllib import error, parse, request


DEFAULT_PROXY = "http://192.168.219.77:8080"
MODES = ("auto", "direct", "proxy", "system")
ROUTE_LABELS = {"proxy": "پروکسی", "direct": "مستقیم/VPN", "system": "پروکسی سیستم"}
_last_good = {}  # proxy setting -> route that worked last time


class TelegramError(Exception):
    code = None  # Telegram/HTTP error code when the server rejected the request


def multipart(fields, files):
    """Build a multipart/form-data body. files = {name: (filename, bytes, content_type)}."""
    boundary = "----majix" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    for key, (filename, content, ctype) in files.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                      f'Content-Type: {ctype}\r\n\r\n').encode("utf-8") + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def normalize_proxy(value):
    value = value.strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    try:
        parts = parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("پورت پروکسی معتبر نیست.") from None
    if (parts.scheme not in ("http", "https") or not parts.hostname or
            not port or parts.path not in ("", "/") or parts.query or parts.fragment or
            parts.username is not None):
        raise ValueError("پروکسی را مانند http://192.168.219.77:8080 وارد کنید؛ برای اتصال مستقیم خالی بگذارید.")
    return parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def redact(text, config):
    text = str(text)
    for key in ("token", "chat_id"):
        value = str(config.get(key, ""))
        if value:
            text = text.replace(value, "[PRIVATE]")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[PRIVATE]", text)[:700]


def route_list(config):
    """Ordered connection routes to try: 'proxy', 'direct' (also works with a VPN), 'system'."""
    mode = config.get("mode", "auto")
    proxy = normalize_proxy(config.get("proxy", DEFAULT_PROXY))
    if mode == "direct":
        return ["direct"]
    if mode == "system":
        return ["system"]
    if mode == "proxy":
        return ["proxy" if proxy else "direct"]
    routes = (["proxy"] if proxy else []) + ["direct"]
    try:
        found = request.getproxies()
        system_proxy = found.get("https") or found.get("http")
    except Exception:
        system_proxy = None
    if system_proxy and system_proxy.rstrip("/") != proxy:
        routes.append("system")
    first = _last_good.get(proxy)
    if first in routes:
        routes.remove(first)
        routes.insert(0, first)
    return routes


def make_opener(config, route=None):
    proxy = normalize_proxy(config.get("proxy", DEFAULT_PROXY))
    route = route or ("proxy" if proxy else "direct")
    if route == "proxy":
        handler = request.ProxyHandler({"http": proxy, "https": proxy})
    elif route == "system":
        handler = request.ProxyHandler()  # environment variables / Windows proxy settings
    else:
        # An empty mapping disables registry and environment proxies: a plain direct
        # connection (this is what a system-wide VPN / TUN adapter needs).
        handler = request.ProxyHandler({})
    return request.build_opener(handler).open


def network_reason(exc, config):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "اعتبار گواهی HTTPS تأیید نشد؛ ساعت ویندوز و پروکسی/VPN را بررسی کنید."
    if isinstance(reason, (TimeoutError,)):
        return "مهلت اتصال تمام شد؛ مسیر تلگرام قطع است (پروکسی/VPN را بررسی کنید)."
    return "خطای شبکه: " + redact(reason, config)


def telegram_call(config, method, payload, stop, opener=None, emit=None, files=None):
    emit = emit or (lambda kind, text: None)
    if opener is not None:
        routes, proxy_key = [(None, opener)], ""
    else:
        routes = [(name, None) for name in route_list(config)]
        proxy_key = normalize_proxy(config.get("proxy", DEFAULT_PROXY))
    multi = len(routes) > 1
    timeout = 10 if multi else 15
    if files:
        timeout = max(timeout, 30)
        data, content_type = multipart({k: str(v) for k, v in payload.items()}, files)
    else:
        data, content_type = json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
    last_error = "پاسخ نامعتبر از تلگرام"
    for attempt in range(3):
        if stop.is_set():
            return None
        emit("log", f"تلگرام: {method}؛ تلاش {attempt + 1}/3")
        delay = 2 ** attempt
        for pos, (name, fixed) in enumerate(routes):
            if stop.is_set():
                return None
            more = pos < len(routes) - 1
            prefix = f"[{ROUTE_LABELS[name]}] " if multi and name else ""
            req = request.Request("https://api.telegram.org/bot" + config["token"] + "/" + method,
                                  data=data, headers={"Content-Type": content_type}, method="POST")
            status = 200
            result = None
            try:
                with (fixed or make_opener(config, name))(req, timeout=timeout) as response:
                    result = json.load(response)
            except error.HTTPError as exc:
                status = exc.code
                try:
                    result = json.load(exc)
                except (ValueError, OSError):
                    result = {}
                finally:
                    exc.close()
            except (error.URLError, TimeoutError, OSError) as exc:
                last_error = prefix + network_reason(exc, config)
            except (ValueError, TypeError):
                last_error = prefix + "پاسخ JSON معتبر دریافت نشد؛ تنظیم پروکسی را بررسی کنید."
            if isinstance(result, dict):
                if result.get("ok") is True:
                    if name:
                        _last_good[proxy_key] = name
                        if multi:
                            emit("log", "مسیر موفق: " + ROUTE_LABELS[name])
                    return result
                code = result.get("error_code", status)
                description = redact(result.get("description", ""), config)
                hints = {
                    400: "Chat ID و شروع گفت‌وگو با بات را بررسی کنید.",
                    401: "توکن بات نامعتبر است.",
                    403: "بات مسدود شده یا اجازهٔ ارسال به این گفت‌وگو ندارد.",
                    404: "توکن یا آدرس بات نامعتبر است.",
                    407: "پروکسی احراز هویت می‌خواهد.",
                }
                last_error = (prefix + f"خطای تلگرام/پروکسی {code}: {hints.get(code, '')} {description}").strip()
                if code == 429:
                    try:
                        delay = max(1, int(result.get("parameters", {}).get("retry_after", 5)))
                    except (ValueError, TypeError, AttributeError):
                        delay = 5
                    emit("log", last_error)
                    break
                if code in hints or (isinstance(code, int) and 400 <= code < 500):
                    # A non-Telegram reply (empty body) or 407 means this route is broken: try the next one.
                    if more and (not result or code == 407):
                        emit("log", last_error)
                        continue
                    failure = TelegramError(last_error)
                    failure.code = code if isinstance(code, int) else None
                    raise failure
            emit("log", last_error)
        if attempt < 2:
            emit("log", f"تلاش مجدد پس از {delay} ثانیه؛ دکمهٔ توقف قابل استفاده است.")
            if stop.wait(delay):
                return None
    raise TelegramError("ارسال پس از ۳ تلاش ناموفق بود. " + last_error)


def send_telegram(config, text, stop, opener=None, emit=None, parse_mode=None):
    payload = {"chat_id": config["chat_id"], "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
        payload["disable_web_page_preview"] = True
    return telegram_call(config, "sendMessage", payload, stop, opener, emit) is not None


def send_photo(config, photo, caption, stop, opener=None, emit=None, parse_mode=None):
    payload = {"chat_id": config["chat_id"], "caption": caption}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    files = {"photo": ("chart.jpg", photo, "image/jpeg")}
    return telegram_call(config, "sendPhoto", payload, stop, opener, emit, files=files) is not None


def test_connection(config, stop, emit):
    names = {"auto": "خودکار", "direct": "مستقیم / VPN", "proxy": "فقط پروکسی", "system": "پروکسی سیستم"}
    emit("log", "حالت اتصال: " + names.get(config.get("mode", "auto"), "؟"))
    emit("log", "مسیرها به‌ترتیب: " + " ← ".join(ROUTE_LABELS[r] for r in route_list(config)))
    if telegram_call(config, "getMe", {}, stop, emit=emit) is None:
        return False
    emit("log", "توکن بات توسط تلگرام تأیید شد؛ در حال ارسال پیام تست...")
    return send_telegram(config, "✅ <b>MAJIX</b>\nتست اتصال با موفقیت انجام شد.\nاین پیام سیگنال معاملاتی نیست.",
                         stop, emit=emit, parse_mode="HTML")
