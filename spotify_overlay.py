import ctypes
import ctypes.wintypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import winreg
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from logging.handlers import RotatingFileHandler

import keyboard
import pystray
import requests
import spotipy
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageTk
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError, SpotifyStateError, start_local_http_server

# ustawienie katalogu roboczego na ten, w którym znajduje się skrypt, aby poprawnie ładować .env i .cache
SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
os.chdir(SCRIPT_DIR)
load_dotenv()

APP_NAME = "Spotify Volume Overlay"
LOG_FILE = os.path.join(SCRIPT_DIR, "debug_overlay.log")


# ---------------------------------------------------------------------------
# logowanie
# ---------------------------------------------------------------------------

# filtr, który nie zapisuje tego samego komunikatu częściej niż raz na `window` sekund
# (wcześniej jeden błąd potrafił zapełnić log ponad tysiącem identycznych linii)
class RepeatFilter(logging.Filter):
    def __init__(self, window: float = 600):
        super().__init__()
        self.window = window
        self._last_written: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        now = time.monotonic()
        with self._lock:
            last = self._last_written.get(msg)
            if last is not None and now - last < self.window:
                self._suppressed[msg] = self._suppressed.get(msg, 0) + 1
                return False
            self._last_written[msg] = now
            skipped = self._suppressed.pop(msg, 0)
            # sprzątamy stare wpisy, żeby słownik nie rósł w nieskończoność
            if len(self._last_written) > 100:
                for key, ts in list(self._last_written.items()):
                    if now - ts >= self.window:
                        del self._last_written[key]
        if skipped:
            record.msg = f"{msg} (pominięto {skipped} powtórzeń)"
            record.args = ()
        return True


# log rotowany: maks. 512 KB + 2 starsze pliki
_log_handler = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_log_handler.addFilter(RepeatFilter())
logging.basicConfig(level=logging.WARNING, handlers=[_log_handler])

# pythonw nie ma konsoli, więc nieobsłużone wyjątki trafiają do logu zamiast znikać
sys.excepthook = lambda t, v, tb: logging.critical("Nieobsłużony wyjątek", exc_info=(t, v, tb))
threading.excepthook = lambda a: logging.critical(
    f"Nieobsłużony wyjątek w wątku {a.thread.name if a.thread else '?'}",
    exc_info=(a.exc_type, a.exc_value, a.exc_traceback),
)


def message_box(text: str, error: bool = True):
    # okienko systemowe – jedyny sposób, żeby pokazać komunikat przy uruchomieniu przez pythonw
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10 if error else 0x40)


# ---------------------------------------------------------------------------
# konfiguracja (.env)
# ---------------------------------------------------------------------------

def _env_number(name: str, default, lo, hi, cast):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw.replace(",", "."))
    except ValueError:
        logging.warning(f"Nieprawidłowa wartość {name}={raw!r}, używam {default}")
        return default
    if not lo <= value <= hi:
        logging.warning(f"{name}={raw} poza zakresem {lo}–{hi}, używam {default}")
        return default
    return value


def _env_choice(name: str, default: str, choices: set[str]) -> str:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw not in choices:
        logging.warning(f"Nieprawidłowa wartość {name}={raw!r} (dozwolone: {', '.join(sorted(choices))}), używam {default}")
        return default
    return raw


CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")

DEFAULT_HOTKEY_UP = "ctrl+alt+up"
DEFAULT_HOTKEY_DOWN = "ctrl+alt+down"
HOTKEY_VOLUME_UP = os.getenv("HOTKEY_VOLUME_UP", "").strip() or DEFAULT_HOTKEY_UP
HOTKEY_VOLUME_DOWN = os.getenv("HOTKEY_VOLUME_DOWN", "").strip() or DEFAULT_HOTKEY_DOWN
VOLUME_STEP = _env_number("VOLUME_STEP", 5, 1, 50, int)
# precyzyjna regulacja (domyślnie o 1%) – ten sam skrót z dodanym Shiftem
DEFAULT_HOTKEY_UP_FINE = "ctrl+alt+shift+up"
DEFAULT_HOTKEY_DOWN_FINE = "ctrl+alt+shift+down"
HOTKEY_VOLUME_UP_FINE = os.getenv("HOTKEY_VOLUME_UP_FINE", "").strip() or DEFAULT_HOTKEY_UP_FINE
HOTKEY_VOLUME_DOWN_FINE = os.getenv("HOTKEY_VOLUME_DOWN_FINE", "").strip() or DEFAULT_HOTKEY_DOWN_FINE
VOLUME_STEP_FINE = _env_number("VOLUME_STEP_FINE", 1, 1, 50, int)
OVERLAY_TIMEOUT = _env_number("OVERLAY_TIMEOUT", 3.0, 0.5, 60.0, float)
OVERLAY_POSITION = _env_choice("OVERLAY_POSITION", "top-left", {"top-left", "top-right", "bottom-left", "bottom-right"})
OVERLAY_MARGIN = _env_number("OVERLAY_MARGIN", 50, 0, 2000, int)
# maks. 0.99 – przy alpha = 1.0 Tk zdejmuje z okna styl WS_EX_LAYERED i kliknięcia przestają przez nie przechodzić
OVERLAY_OPACITY = _env_number("OVERLAY_OPACITY", 0.96, 0.2, 0.99, float)
OVERLAY_FADE_MS = _env_number("OVERLAY_FADE_MS", 150, 0, 2000, int)

if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI]):
    logging.critical("Brak zmiennych SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET / SPOTIPY_REDIRECT_URI w .env")
    message_box("W pliku .env brakuje danych Spotify API:\nSPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI")
    sys.exit(1)


# ---------------------------------------------------------------------------
# tylko jedna instancja (autostart + ręczne uruchomienie dałyby podwójne skróty klawiszowe)
# ---------------------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
_instance_mutex = _kernel32.CreateMutexW(None, False, "Local\\SpotifyVolumeOverlay")
if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
    message_box("Nakładka już działa – jej ikona jest w zasobniku systemowym.", error=False)
    sys.exit(0)


# ---------------------------------------------------------------------------
# autoryzacja Spotify
# ---------------------------------------------------------------------------

SCOPE = "user-read-playback-state,user-modify-playback-state"
AUTH_TIMEOUT = 180  # ile sekund czekamy na zalogowanie w przeglądarce


# SpotifyOAuth, który sam naprawia unieważniony/wygasły refresh token (invalid_grant):
# domyślnie spotipy w takiej sytuacji rzuca wyjątek przy każdym zapytaniu i nigdy nie prosi o ponowne logowanie,
# więc nakładka po cichu przestaje działać. Tutaj usuwamy zepsuty .cache i uruchamiamy logowanie w przeglądarce.
# Jeśli logowanie się nie uda, nie otwieramy przeglądarki przy każdym klawiszu – czekamy na "Zaloguj ponownie" w zasobniku.
class SelfHealingOAuth(SpotifyOAuth):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._auth_lock = threading.Lock()  # zapobiega równoległym logowaniom (konflikt portu 8888)
        self.login_required = False
        self.on_login_start = None          # callback wywoływany przed otwarciem przeglądarki

    def _clear_cache(self):
        cache_path = getattr(self.cache_handler, "cache_path", None)
        if cache_path and os.path.exists(cache_path):
            os.remove(cache_path)

    # wersja z timeoutem i zamykaniem portu – oryginał czeka w nieskończoność, gdy ktoś zamknie przeglądarkę
    def _get_auth_response_local_server(self, redirect_port):
        server = start_local_http_server(redirect_port)
        server.timeout = AUTH_TIMEOUT
        try:
            self._open_auth_url()
            server.handle_request()
        finally:
            server.server_close()

        if server.error is not None:
            raise server.error
        if self.state is not None and server.state != self.state:
            raise SpotifyStateError(self.state, server.state)
        if server.auth_code is not None:
            return server.auth_code
        raise SpotifyOauthError("Nie zalogowano w przeglądarce w wyznaczonym czasie", error="login_timeout")

    def _interactive_login(self, as_dict):
        logging.warning("Otwieram logowanie do Spotify w przeglądarce")
        if self.on_login_start:
            self.on_login_start()
        try:
            token = super().get_access_token(as_dict=as_dict, check_cache=False)
        except Exception:
            self.login_required = True
            raise
        self.login_required = False
        return token

    def get_access_token(self, code=None, as_dict=True, check_cache=True):
        with self._auth_lock:
            if self.login_required:
                raise SpotifyOauthError("Wymagane ponowne logowanie do Spotify", error="login_required")
            if self.cache_handler.get_cached_token():
                try:
                    return super().get_access_token(code=code, as_dict=as_dict, check_cache=check_cache)
                except SpotifyOauthError as e:
                    if getattr(e, "error", None) != "invalid_grant":
                        # spotipy samo mogło otworzyć logowanie (np. zmiana uprawnień) – po nieudanym nie ponawiamy
                        if getattr(e, "error", None) == "login_timeout" or isinstance(e, SpotifyStateError):
                            self.login_required = True
                        raise
                    logging.error(f"Token Spotify nieważny ({e.error_description}), loguję ponownie")
                    self._clear_cache()
            return self._interactive_login(as_dict)

    # ręczne ponowne logowanie (z menu w zasobniku)
    def relogin(self):
        with self._auth_lock:
            self.login_required = False
            self._clear_cache()
            return self._interactive_login(as_dict=False)


auth_manager = SelfHealingOAuth(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                                redirect_uri=REDIRECT_URI, scope=SCOPE, open_browser=True)
# krótkie timeouty i bez długiego ponawiania – lepiej szybko pokazać błąd niż wisieć
sp = spotipy.Spotify(auth_manager=auth_manager, requests_timeout=5, retries=1, status_retries=0)


# ---------------------------------------------------------------------------
# model danych odtwarzania
# ---------------------------------------------------------------------------

@dataclass
class Playback:
    track_id: str | None
    title: str
    artist: str
    volume: int | None          # None = urządzenie nie pozwala zmieniać głośności
    cover_url: str | None
    is_playing: bool
    progress_ms: int
    duration_ms: int
    fetched_at: float           # time.monotonic() w chwili pobrania – do lokalnego liczenia postępu

    # po tym rozpoznajemy, czy klawisz multimedialny już "zadziałał" po stronie API
    @property
    def signature(self):
        return (self.track_id or self.title, self.is_playing)

    def progress_now(self) -> int:
        progress = self.progress_ms
        if self.is_playing:
            progress += int((time.monotonic() - self.fetched_at) * 1000)
        return max(0, min(progress, self.duration_ms)) if self.duration_ms else progress


def parse_playback(data: dict | None) -> Playback | None:
    if not data:
        return None  # brak aktywnego urządzenia
    device = data.get("device") or {}
    volume = device.get("volume_percent") if device.get("supports_volume", True) else None
    item = data.get("item")

    if not item:
        is_ad = data.get("currently_playing_type") == "ad"
        return Playback(None, "Reklama" if is_ad else "Nieznany utwór", "Spotify", volume, None,
                        bool(data.get("is_playing")), 0, 0, time.monotonic())

    if item.get("type") == "episode":
        show = item.get("show") or {}
        artist = show.get("name", "")
        images = item.get("images") or show.get("images") or []
    else:
        artist = ", ".join(a.get("name", "") for a in item.get("artists", []))
        images = (item.get("album") or {}).get("images") or []

    # Spotify zwraca zwykle 640, 300 i 64 px – bierzemy środkowy rozmiar
    cover_url = images[min(1, len(images) - 1)]["url"] if images else None

    return Playback(
        track_id=item.get("id") or item.get("uri"),
        title=item.get("name") or "Nieznany utwór",
        artist=artist,
        volume=volume,
        cover_url=cover_url,
        is_playing=bool(data.get("is_playing")),
        progress_ms=data.get("progress_ms") or 0,
        duration_ms=item.get("duration_ms") or 0,
        fetched_at=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# status aplikacji i opisy błędów
# ---------------------------------------------------------------------------

class AppStatus:
    def __init__(self):
        self._lock = threading.Lock()
        self.ok: bool | None = None          # None = łączenie / logowanie
        self.text = "Łączenie ze Spotify…"
        self.listeners = []

    def set(self, ok: bool | None, text: str):
        with self._lock:
            if (ok, text) == (self.ok, self.text):
                return
            self.ok, self.text = ok, text
        for callback in self.listeners:
            try:
                callback()
            except Exception as e:
                logging.error(f"Błąd aktualizacji statusu: {e}")


status = AppStatus()


def _short(text: str, limit: int = 70) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def describe_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, SpotifyOauthError):
        if getattr(exc, "error", None) == "login_timeout":
            return "Nie dokończono logowania", "Zasobnik → Zaloguj ponownie do Spotify"
        return "Wymagane logowanie do Spotify", "Zasobnik → Zaloguj ponownie do Spotify"
    if isinstance(exc, SpotifyException):
        reason = (exc.reason or "").upper()
        if exc.http_status == 404:
            return "Brak aktywnego urządzenia", "Włącz odtwarzanie w aplikacji Spotify"
        if exc.http_status == 403:
            if "PREMIUM" in reason or "premium" in str(exc.msg).lower():
                return "Wymagane Spotify Premium", "Sterowanie działa tylko z kontem Premium"
            if "VOLUME" in reason:
                return "Nie można zmienić głośności", "To urządzenie nie obsługuje regulacji"
            return "Spotify odrzucił polecenie", _short(exc.msg)
        if exc.http_status == 401:
            return "Sesja Spotify wygasła", "Zasobnik → Zaloguj ponownie do Spotify"
        if exc.http_status == 429:
            return "Za dużo zapytań do Spotify", "Spróbuj ponownie za chwilę"
        if exc.http_status >= 500:
            return "Serwery Spotify nie odpowiadają", "Spróbuj ponownie za chwilę"
        return "Błąd Spotify API", _short(exc.msg)
    if isinstance(exc, requests.exceptions.RequestException):
        return "Brak połączenia ze Spotify", "Sprawdź połączenie z internetem"
    return "Nieoczekiwany błąd", _short(exc)


def _is_soft_error(exc: Exception) -> bool:
    # brak urządzenia / brak Premium to nie awaria aplikacji – ikona w zasobniku zostaje zielona
    return isinstance(exc, SpotifyException) and exc.http_status in (403, 404)


def report_ok():
    status.set(True, "Połączono ze Spotify")


def report_error(exc: Exception, context: str, show: bool = True):
    title, hint = describe_error(exc)
    if _is_soft_error(exc):
        logging.info(f"{context}: {exc}")
    else:
        logging.error(f"{context}: {exc}")
        status.set(False, title)
    if show:
        ui(lambda: overlay.show_message(title, hint))


# ---------------------------------------------------------------------------
# grafika: placeholdery okładek i ikona zasobnika
# ---------------------------------------------------------------------------

def _symbol_font(size: int):
    try:
        return ImageFont.truetype("seguisym.ttf", size)
    except Exception:
        return ImageFont.load_default()


def make_placeholder(symbol: str = "♪", color: str = "#666666", size: int = 100) -> Image.Image:
    img = Image.new("RGB", (size, size), "#2a2a2a")
    draw = ImageDraw.Draw(img)
    font = _symbol_font(int(size * 0.48))
    bbox = draw.textbbox((0, 0), symbol, font=font)
    x = (size - (bbox[2] - bbox[0])) // 2 - bbox[0]
    y = (size - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x, y), symbol, fill=color, font=font)
    return img


def make_tray_image(ok: bool | None) -> Image.Image:
    color = {True: "#1DB954", False: "#E5534B", None: "#8A8A8A"}[ok]
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=color)
    font = _symbol_font(38)
    bbox = draw.textbbox((0, 0), "♪", font=font)
    x = (64 - (bbox[2] - bbox[0])) // 2 - bbox[0]
    y = (64 - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x, y), "♪", fill="white", font=font)
    return img


# ---------------------------------------------------------------------------
# pomocnicze: tekst, czas, położenie
# ---------------------------------------------------------------------------

def ellipsize(font: tkfont.Font, text: str, width: int, force: bool = False) -> str:
    if not force and font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


# zawija tekst do max_lines linii mieszczących się w width pikseli, a nadmiar zastępuje "…"
def fit_lines(font: tkfont.Font, text: str, width: int, max_lines: int) -> str:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if font.measure(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        # słowo dłuższe niż cała linia – łamiemy je po znakach
        while font.measure(word) > width and len(word) > 1:
            cut = len(word) - 1
            while cut > 1 and font.measure(word[:cut]) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        rest = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [ellipsize(font, rest, width, force=True)]
    return "\n".join(lines)


def format_ms(ms: int) -> str:
    seconds = max(0, ms) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def work_area() -> tuple[int, int, int, int]:
    # obszar ekranu bez paska zadań (SPI_GETWORKAREA)
    rect = ctypes.wintypes.RECT()
    if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    user32 = ctypes.windll.user32
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


# ---------------------------------------------------------------------------
# kolejka zadań dla wątku GUI – tkinter nie jest bezpieczny wątkowo, więc inne wątki
# nie dotykają widgetów bezpośrednio, tylko wrzucają funkcje do kolejki
# ---------------------------------------------------------------------------

_ui_queue: "queue.SimpleQueue" = queue.SimpleQueue()


def ui(fn):
    _ui_queue.put(fn)


# ---------------------------------------------------------------------------
# nakładka
# ---------------------------------------------------------------------------

BG = "#1f1f1f"
WIDTH, HEIGHT = 380, 120
TEXT_X, TEXT_W = 170, 195
PLACEHOLDER = "__placeholder__"
WARNING = "__warning__"


class VolumeOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg=BG)
        self.root.report_callback_exception = lambda t, v, tb: logging.error("Błąd GUI", exc_info=(t, v, tb))
        self._place_window()

        # okno musi zostać zmapowane (jest niewidoczne, bo alpha = 0), żeby mieć poprawne ID przed zmianą stylów
        self.root.update()
        self.hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        self._apply_click_through()
        self.root.withdraw()

        self.session = requests.Session()  # jedna sesja do pobierania okładek
        self._covers: "OrderedDict[str, ImageTk.PhotoImage]" = OrderedDict()  # cache okładek (ostatnie 30)
        self._wanted_cover = ""
        self._playback: Playback | None = None
        self._visible = False
        self._hide_job = None
        self._fade_job = None
        self._tick_job = None

        self._build_widgets()
        self.root.after(30, self._pump)

    def _place_window(self):
        left, top, right, bottom = work_area()
        x = left + OVERLAY_MARGIN if "left" in OVERLAY_POSITION else right - OVERLAY_MARGIN - WIDTH
        y = top + OVERLAY_MARGIN if "top" in OVERLAY_POSITION else bottom - OVERLAY_MARGIN - HEIGHT
        self.root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")

    # okno "przenikające" – kliknięcia przechodzą przez nie do aplikacji pod spodem
    def _apply_click_through(self):
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080  # bez przycisku na pasku zadań / w Alt+Tab
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        wanted = style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        if wanted != style:
            user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, wanted)

    def _build_widgets(self):
        self.title_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.small_font = tkfont.Font(family="Segoe UI", size=9)

        # pasek głośności (pionowy)
        self.vol_canvas_height = 80
        self.vol_canvas = tk.Canvas(self.root, bg="#404040", highlightthickness=0)
        self.vol_canvas.place(x=15, y=15, width=12, height=self.vol_canvas_height)
        self.vol_bar = self.vol_canvas.create_rectangle(
            0, self.vol_canvas_height, 12, self.vol_canvas_height, fill="#cccccc", width=0
        )

        # wartość procentowa głośności – pod paskiem
        self.vol_number_label = tk.Label(self.root, text="--", font=self.small_font, fg="white", bg=BG, anchor="center")
        self.vol_number_label.place(x=0, y=98, width=42)

        # okładka
        self.cover_label = tk.Label(self.root, bg=BG)
        self.cover_label.place(x=60, y=10, width=100, height=100)

        # tytuł (do 2 linii) i wykonawca
        self.title_label = tk.Label(self.root, text="Spotify", font=self.title_font, fg="white", bg=BG,
                                    anchor="nw", justify="left")
        self.title_label.place(x=TEXT_X, y=8, width=TEXT_W)
        self.artist_label = tk.Label(self.root, text="", font=self.small_font, fg="#b3b3b3", bg=BG, anchor="nw")
        self.artist_label.place(x=TEXT_X, y=32, width=TEXT_W)

        # stan odtwarzania: rysowana ikona play/pauza + czas, a pod spodem pasek postępu
        self.state_icon = tk.Canvas(self.root, width=10, height=10, bg=BG, highlightthickness=0)
        self.state_label = tk.Label(self.root, text="", font=self.small_font, fg="#b3b3b3", bg=BG, anchor="nw")
        self.state_label.place(x=TEXT_X + 16, y=80, width=TEXT_W - 16)
        self.progress_canvas = tk.Canvas(self.root, bg="#404040", highlightthickness=0)
        self.progress_canvas.place(x=TEXT_X, y=104, width=TEXT_W, height=4)
        self.progress_bar = self.progress_canvas.create_rectangle(0, 0, 0, 4, fill="#cccccc", width=0)

    # przetwarza zadania wrzucone przez inne wątki
    def _pump(self):
        try:
            while True:
                fn = _ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    logging.exception("Błąd podczas aktualizacji nakładki")
        except queue.Empty:
            pass
        self.root.after(30, self._pump)

    # ---- publiczne metody (wywoływać tylko z wątku GUI, czyli przez ui(...)) ----

    def show_playback(self, playback: Playback):
        self._playback = playback
        self._set_volume(playback.volume)
        self._set_texts(playback.title, playback.artist)
        self._set_cover(playback.cover_url or PLACEHOLDER)
        self._update_progress()
        self._show()

    # szybka aktualizacja samej głośności (od razu po naciśnięciu skrótu, zanim odpowie API)
    def show_volume(self, volume: int):
        self._set_volume(volume)
        self._show()

    def show_message(self, title: str, hint: str):
        self._playback = None
        self._set_texts(title, hint)
        self._set_cover(WARNING)
        self._update_progress()
        self._show()

    # ---- elementy nakładki ----

    def _set_volume(self, volume: int | None):
        if volume is None:
            self.vol_number_label.config(text="--")
            self.vol_canvas.coords(self.vol_bar, 0, self.vol_canvas_height, 12, self.vol_canvas_height)
            return
        self.vol_number_label.config(text=f"{int(volume)}%")
        top_y = self.vol_canvas_height - int(volume / 100 * self.vol_canvas_height)
        self.vol_canvas.coords(self.vol_bar, 0, top_y, 12, self.vol_canvas_height)

    def _set_texts(self, title: str, subtitle: str):
        title_text = fit_lines(self.title_font, title, TEXT_W, max_lines=2)
        self.title_label.config(text=title_text)
        lines = title_text.count("\n") + 1
        self.artist_label.place_configure(y=8 + lines * self.title_font.metrics("linespace") + 2)
        self.artist_label.config(text=ellipsize(self.small_font, subtitle, TEXT_W))

    def _update_progress(self):
        if self._tick_job:
            self.root.after_cancel(self._tick_job)
            self._tick_job = None
        p = self._playback
        if p is None:
            self.state_label.config(text="")
            self.state_icon.place_forget()
            self.progress_canvas.place_forget()
            return
        self._draw_state_icon(p.is_playing)
        progress = p.progress_now()
        if p.duration_ms:
            self.state_label.config(text=f"{format_ms(progress)} / {format_ms(p.duration_ms)}")
            self.progress_canvas.place(x=TEXT_X, y=104, width=TEXT_W, height=4)
            self.progress_canvas.coords(self.progress_bar, 0, 0, int(TEXT_W * progress / p.duration_ms), 4)
        else:
            self.state_label.config(text="")
            self.progress_canvas.place_forget()
        # postęp liczymy lokalnie, bez dodatkowych zapytań do API
        if p.is_playing and self._visible:
            self._tick_job = self.root.after(500, self._update_progress)

    # pokazujemy stan, który jest teraz: trójkąt = gra, dwie kreski = pauza
    def _draw_state_icon(self, playing: bool):
        c = self.state_icon
        c.delete("all")
        color = "#b3b3b3"
        if playing:
            c.create_polygon(1, 0, 1, 10, 10, 5, fill=color, outline="")
        else:
            c.create_rectangle(1, 0, 4, 10, fill=color, width=0)
            c.create_rectangle(6, 0, 9, 10, fill=color, width=0)
        # wyśrodkowanie ikony względem cyfr czasu (+2 px to wewnętrzny odstęp etykiety)
        y = 80 + 2 + (self.small_font.metrics("linespace") - 10) // 2
        c.place(x=TEXT_X + 1, y=y, width=10, height=10)

    def _set_cover(self, key: str):
        self._wanted_cover = key
        if key in self._covers:
            self._covers.move_to_end(key)
            self._apply_cover(key)
        elif key == PLACEHOLDER:
            self._store_cover(key, make_placeholder("♪", "#666666"))
            self._apply_cover(key)
        elif key == WARNING:
            self._store_cover(key, make_placeholder("⚠", "#E5A50A"))
            self._apply_cover(key)
        else:
            threading.Thread(target=self._download_cover, args=(key,), daemon=True).start()

    def _download_cover(self, url: str):
        try:
            response = self.session.get(url, timeout=5)
            response.raise_for_status()
            image = Image.open(BytesIO(response.content)).convert("RGB").resize((100, 100), Image.Resampling.LANCZOS)
        except Exception as e:
            logging.error(f"Błąd pobierania okładki ({url}): {e}")
            image = None
        ui(lambda: self._cover_downloaded(url, image))

    def _cover_downloaded(self, url: str, image: Image.Image | None):
        if image is None:
            if self._wanted_cover == url:
                self._set_cover(PLACEHOLDER)
            return
        self._store_cover(url, image)
        if self._wanted_cover == url:  # ignoruj, jeśli w międzyczasie zmienił się utwór
            self._apply_cover(url)

    def _store_cover(self, key: str, image: Image.Image):
        self._covers[key] = ImageTk.PhotoImage(image)
        while len(self._covers) > 30:
            self._covers.popitem(last=False)

    def _apply_cover(self, key: str):
        img = self._covers[key]
        self.cover_label.config(image=img)
        self.cover_label.image = img  # referencja chroni obraz przed garbage collectorem

    # ---- pokazywanie / ukrywanie z płynnym przejściem ----

    def _show(self):
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
        if not self._visible:
            self._visible = True
            self.root.deiconify()
            self._apply_click_through()
            self._update_progress()
        self._fade_to(OVERLAY_OPACITY)
        self._hide_job = self.root.after(int(OVERLAY_TIMEOUT * 1000), self._hide)

    def _hide(self):
        self._hide_job = None
        self._fade_to(0.0, on_done=self._withdraw)

    def _withdraw(self):
        self.root.withdraw()
        self._visible = False
        if self._tick_job:
            self.root.after_cancel(self._tick_job)
            self._tick_job = None

    def _fade_to(self, target: float, on_done=None):
        if self._fade_job:
            self.root.after_cancel(self._fade_job)
            self._fade_job = None
        start = float(self.root.attributes("-alpha"))
        steps = max(1, OVERLAY_FADE_MS // 15)

        def step(i: int):
            self.root.attributes("-alpha", start + (target - start) * i / steps)
            if i < steps:
                self._fade_job = self.root.after(15, step, i + 1)
            else:
                self._fade_job = None
                if on_done:
                    on_done()

        step(1)

    def start(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# komunikacja ze Spotify
# ---------------------------------------------------------------------------

_FAILED = object()          # znacznik "zapytanie się nie udało" (w odróżnieniu od None = nic nie gra)
_last_signature = None      # (track_id, is_playing) ostatnio pokazanego stanu


def fetch_playback(context: str, show_errors: bool = True):
    try:
        data = sp.current_playback(additional_types="episode")
    except Exception as e:
        report_error(e, context, show=show_errors)
        return _FAILED
    report_ok()
    return parse_playback(data)


def publish(playback: Playback, sync_volume: bool = True):
    global _last_signature
    _last_signature = playback.signature
    if sync_volume:
        volume_ctl.sync(playback.volume)
    ui(lambda: overlay.show_playback(playback))


def show_no_device():
    ui(lambda: overlay.show_message("Nic nie jest odtwarzane", "Włącz odtwarzanie w aplikacji Spotify"))


RETRY_INTERVAL = 0.3        # odstęp między kolejnymi próbami, gdy API jeszcze nie zaktualizowało stanu
MAX_RETRIES = 3             # maksymalna liczba prób weryfikacji zmiany utworu / pauzy


# pobiera stan odtwarzania i pokazuje nakładkę; expect_change=True po klawiszu multimedialnym –
# wtedy czekamy chwilę, aż API odda nowy utwór albo zmieniony stan play/pause
def refresh_overlay(expect_change: bool = False):
    previous = _last_signature
    for attempt in range(MAX_RETRIES + 1):
        playback = fetch_playback("Odświeżanie nakładki")
        if playback is _FAILED:
            return
        if playback is None:
            show_no_device()
            return
        if expect_change and previous and playback.signature == previous and attempt < MAX_RETRIES:
            time.sleep(RETRY_INTERVAL)
            continue
        publish(playback)
        return


# ---------------------------------------------------------------------------
# sterowanie głośnością
# ---------------------------------------------------------------------------

# Głośność trzymamy lokalnie: naciśnięcie skrótu od razu zmienia pasek na nakładce, a do Spotify
# wysyłamy tylko ostatnią wartość (najwyżej raz na SEND_INTERVAL). Dzięki temu szybkie/przytrzymane
# naciśnięcia nie "gubią" kroków i nie zalewają API zapytaniami.
class VolumeController:
    SEND_INTERVAL = 0.15    # s – zbieranie naciśnięć w jedno zapytanie
    CACHE_TTL = 5.0         # s – po tym czasie bez zmian odczytujemy głośność z API na nowo

    def __init__(self):
        self._cond = threading.Condition()
        self._volume: int | None = None
        self._fresh_until = 0.0
        self._pending_delta = 0
        self._dirty = False
        threading.Thread(target=self._run, daemon=True, name="volume").start()

    def nudge(self, delta: int):
        with self._cond:
            now = time.monotonic()
            if self._volume is not None and now < self._fresh_until and not self._pending_delta:
                self._volume = max(0, min(100, self._volume + delta))
                self._fresh_until = now + self.CACHE_TTL
                self._dirty = True
                volume = self._volume
            else:
                self._pending_delta += delta  # najpierw trzeba odczytać aktualną głośność
                volume = None
            self._cond.notify()
        if volume is not None:
            ui(lambda: overlay.show_volume(volume))

    # aktualizacja lokalnej wartości na podstawie świeżych danych z API
    def sync(self, volume: int | None):
        with self._cond:
            if self._dirty or self._pending_delta:
                return
            self._volume = volume
            self._fresh_until = time.monotonic() + self.CACHE_TTL if volume is not None else 0.0

    def _invalidate(self):
        with self._cond:
            self._volume = None
            self._fresh_until = 0.0
            self._pending_delta = 0
            self._dirty = False

    def _fetch(self) -> bool:
        playback = fetch_playback("Odczyt głośności")
        if playback is _FAILED or playback is None or playback.volume is None:
            self._invalidate()
            if playback is None:
                show_no_device()
            elif playback is not _FAILED:
                ui(lambda: overlay.show_message("Nie można zmienić głośności", "To urządzenie nie obsługuje regulacji"))
            return False
        with self._cond:
            self._volume = max(0, min(100, playback.volume + self._pending_delta))
            self._pending_delta = 0
            self._dirty = True
            self._fresh_until = time.monotonic() + self.CACHE_TTL
            playback.volume = self._volume
        publish(playback, sync_volume=False)
        return True

    def _run(self):
        while True:
            with self._cond:
                while not self._dirty and not self._pending_delta:
                    self._cond.wait()
                need_fetch = bool(self._pending_delta)
            if need_fetch and not self._fetch():
                continue
            time.sleep(self.SEND_INTERVAL)
            with self._cond:
                volume = self._volume
                self._dirty = False
            if volume is None:
                continue
            try:
                sp.volume(volume)
                report_ok()
            except Exception as e:
                self._invalidate()
                report_error(e, "Zmiana głośności")


# ---------------------------------------------------------------------------
# klawisze multimedialne (throttling)
# ---------------------------------------------------------------------------

_throttle_lock = threading.Lock()
_throttle_active = False    # czy aktualnie trwa okno throttlingu
_throttle_pending = False   # czy przyszło zdarzenie podczas aktywnego okna
MIN_INTERVAL = 0.5          # minimalna przerwa między zapytaniami do API (s)


def _throttled_refresh():
    global _throttle_active, _throttle_pending
    with _throttle_lock:
        if _throttle_active:
            _throttle_pending = True  # zapamiętaj zdarzenie, nie resetuj timera
            return
        _throttle_active = True
        _throttle_pending = False

    def _worker():
        global _throttle_active, _throttle_pending
        time.sleep(MIN_INTERVAL)   # czekaj na propagację zmiany w Spotify
        refresh_overlay(expect_change=True)
        with _throttle_lock:
            pending = _throttle_pending
            _throttle_active = False
            _throttle_pending = False
        if pending:                # było zdarzenie podczas aktywnego throttlingu, więc odśwież ponownie
            time.sleep(MIN_INTERVAL)
            refresh_overlay(expect_change=True)

    threading.Thread(target=_worker, daemon=True).start()


# słowa kluczowe występujące w nazwach klawiszy multimedialnych
MEDIA_KEY_KEYWORDS = frozenset(["track", "media", "play", "pause", "next", "previous"])


def on_any_key(event: keyboard.KeyboardEvent):
    if event.event_type != "down" or not event.name:
        return
    name = event.name.lower()
    if any(word in name for word in MEDIA_KEY_KEYWORDS):
        _throttled_refresh()


def register_hotkey(combo: str, default: str, callback):
    try:
        keyboard.add_hotkey(combo, callback)
    except (ValueError, KeyError) as e:
        logging.error(f"Nieprawidłowy skrót {combo!r} ({e}), używam {default!r}")
        keyboard.add_hotkey(default, callback)


# ---------------------------------------------------------------------------
# autostart z Windows (HKCU\...\Run)
# ---------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SpotifyVolumeOverlay"


def _autostart_command() -> str:
    exe = sys.executable
    folder, name = os.path.split(exe)
    lower = name.lower()
    # przy autostarcie chcemy pythonw (bez okna konsoli)
    if lower.startswith("python") and not lower.startswith("pythonw"):
        candidate = os.path.join(folder, "pythonw" + name[len("python"):])
        if os.path.exists(candidate):
            exe = candidate
    return f'"{exe}" "{SCRIPT_PATH}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False


def set_autostart(enabled: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# ikona w zasobniku
# ---------------------------------------------------------------------------

_relogin_running = threading.Event()


def relogin():
    if _relogin_running.is_set():
        return
    _relogin_running.set()

    def worker():
        try:
            auth_manager.relogin()
            report_ok()
            refresh_overlay()
        except Exception as e:
            report_error(e, "Ponowne logowanie")
        finally:
            _relogin_running.clear()

    threading.Thread(target=worker, daemon=True).start()


def toggle_autostart():
    try:
        set_autostart(not autostart_enabled())
    except OSError as e:
        logging.error(f"Nie udało się zmienić autostartu: {e}")


def open_log():
    if os.path.exists(LOG_FILE):
        os.startfile(LOG_FILE)


def quit_app():
    keyboard.unhook_all()
    tray.stop()
    ui(overlay.root.destroy)


class Tray:
    def __init__(self):
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: status.text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pokaż nakładkę",
                             lambda icon, item: threading.Thread(target=refresh_overlay, daemon=True).start(),
                             default=True),
            pystray.MenuItem("Zaloguj ponownie do Spotify", lambda icon, item: relogin()),
            pystray.MenuItem("Uruchamiaj razem z Windows", lambda icon, item: toggle_autostart(),
                             checked=lambda item: autostart_enabled()),
            pystray.MenuItem("Otwórz log", lambda icon, item: open_log()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Zamknij", lambda icon, item: quit_app()),
        )
        self.icon = pystray.Icon("spotify_overlay", make_tray_image(status.ok), self._tooltip(), menu)

    @staticmethod
    def _tooltip() -> str:
        return f"{APP_NAME}\n{status.text}"[:127]

    def refresh(self):
        self.icon.icon = make_tray_image(status.ok)
        self.icon.title = self._tooltip()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def start(self):
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def startup_check():
    # sprawdzenie tokenu w tle – przy autostarcie sieć może jeszcze nie działać, więc nie blokujemy GUI
    try:
        auth_manager.get_access_token(as_dict=False)
        report_ok()
    except Exception as e:
        report_error(e, "Logowanie przy starcie")


def main():
    global overlay, tray, volume_ctl

    overlay = VolumeOverlay()
    volume_ctl = VolumeController()
    tray = Tray()
    status.listeners.append(tray.refresh)
    auth_manager.on_login_start = lambda: status.set(None, "Logowanie w przeglądarce…")
    tray.start()

    # jeśli autostart jest włączony, odświeżamy wpis (np. po przeniesieniu folderu)
    try:
        if autostart_enabled():
            set_autostart(True)
    except OSError as e:
        logging.error(f"Nie udało się odświeżyć autostartu: {e}")

    register_hotkey(HOTKEY_VOLUME_UP, DEFAULT_HOTKEY_UP, lambda: volume_ctl.nudge(VOLUME_STEP))
    register_hotkey(HOTKEY_VOLUME_DOWN, DEFAULT_HOTKEY_DOWN, lambda: volume_ctl.nudge(-VOLUME_STEP))
    register_hotkey(HOTKEY_VOLUME_UP_FINE, DEFAULT_HOTKEY_UP_FINE, lambda: volume_ctl.nudge(VOLUME_STEP_FINE))
    register_hotkey(HOTKEY_VOLUME_DOWN_FINE, DEFAULT_HOTKEY_DOWN_FINE, lambda: volume_ctl.nudge(-VOLUME_STEP_FINE))
    keyboard.hook(on_any_key)

    threading.Thread(target=startup_check, daemon=True, name="startup").start()

    overlay.start()
    keyboard.unhook_all()
    tray.stop()
    logging.shutdown()


if __name__ == "__main__":
    main()
