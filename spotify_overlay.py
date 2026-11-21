import base64
import ctypes
import ctypes.wintypes
import glob
import logging
import os
import queue
import re
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
import mutagen
import pystray
import requests
import spotipy
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageTk
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "tak"):
        return True
    if raw in ("0", "false", "no", "off", "nie"):
        return False
    logging.warning(f"Nieprawidłowa wartość {name}={raw!r} (dozwolone: true/false), używam {default}")
    return default


def _system_accent() -> str | None:
    # kolor akcentu z ustawień Windows (zapisany jako 0xAABBGGRR)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            value = winreg.QueryValueEx(key, "AccentColor")[0] & 0xFFFFFFFF
        return f"#{value & 0xFF:02x}{(value >> 8) & 0xFF:02x}{(value >> 16) & 0xFF:02x}"
    except OSError:
        return None


def _env_color(name: str, default: str) -> str:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw == "system":
        return _system_accent() or default
    if re.fullmatch(r"#?[0-9a-f]{6}", raw):
        return "#" + raw.lstrip("#")
    logging.warning(f"Nieprawidłowy kolor {name}={raw!r} (np. #0078d4 albo system), używam {default}")
    return default


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
# kolor paska głośności i postępu: #rrggbb albo "system" (akcent z ustawień Windows); domyślnie niebieski Windows
OVERLAY_ACCENT = _env_color("OVERLAY_ACCENT", "#0078d4")
OVERLAY_BLUR = _env_bool("OVERLAY_BLUR", True)  # rozmyta okładka jako tło nakładki
# okładki plików lokalnych: z samego pliku, a gdy jej brak – wyszukiwanie w katalogu Spotify
LOCAL_COVERS = _env_bool("LOCAL_COVERS", True)
LOCAL_COVERS_ONLINE = _env_bool("LOCAL_COVERS_ONLINE", True)
# dodatkowe foldery z muzyką (przeszukiwane rekurencyjnie), oddzielone średnikiem
LOCAL_MUSIC_DIRS = [d.strip() for d in os.getenv("LOCAL_MUSIC_DIRS", "").split(";") if d.strip()]

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
    is_local: bool = False      # plik lokalny – API nie zwraca dla niego okładki
    album: str = ""

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
        is_local=bool(item.get("is_local")),
        album=(item.get("album") or {}).get("name") or "",
    )


# ---------------------------------------------------------------------------
# okładki plików lokalnych
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".opus", ".wav")
FOLDER_COVER_NAMES = ("cover", "folder", "front", "album")
SPOTIFY_USERS_DIR = os.path.join(os.environ.get("APPDATA", ""), "Spotify", "Users")
_LOCAL_PATH_RE = re.compile(
    r"[A-Za-z]:\\[^\x00-\x1f�]{1,260}?\.(?:" + "|".join(e[1:] for e in AUDIO_EXTENSIONS) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.casefold()).split())


# normalizacja tytułu do porównań: bez wielkości liter, interpunkcji, "(feat. ...)", "[Remastered]" itp.
def _norm(text: str) -> str:
    text = text or ""
    stripped = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    stripped = re.sub(r"\b(feat|ft|featuring)\b.*", " ", stripped, flags=re.IGNORECASE)
    return _clean(stripped) or _clean(text)


def _artist_set(text: str) -> set[str]:
    parts = re.split(r",|&|;|/|\bx\b|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\bwith\b", (text or "").casefold())
    return {c for c in (_clean(p) for p in parts) if c}


# warianty nazwy pliku: "05 - Tytuł", "05.-Tytuł", "Wykonawca - Tytuł"
def _filename_titles(path: str) -> set[str]:
    stem = os.path.splitext(os.path.basename(path))[0]
    no_number = re.sub(r"^\s*\d{1,3}\s*[.\-_)]*\s*", "", stem)
    variants = {_norm(stem), _norm(no_number)}
    if " - " in no_number:
        variants.add(_norm(no_number.split(" - ")[-1]))
    return {v for v in variants if v}


@dataclass
class LocalTrack:
    path: str
    titles: set[str]        # tytuł z tagów + warianty nazwy pliku
    artists: set[str]
    album: str
    duration_ms: int
    tag_artist: str
    tag_album: str


# Spotify nie podaje przez API ani okładki, ani ścieżki pliku lokalnego, ale trzyma własny indeks
# (local-files.bnk) ze ścieżkami. Na tej podstawie znajdujemy plik po tagach i wyciągamy z niego okładkę.
class LocalCoverResolver:
    RESCAN_INTERVAL = 60.0  # s – najczęściej tak skanujemy foldery ponownie, gdy utwór nie zostanie znaleziony

    def __init__(self, extra_dirs: list[str], online: bool):
        self.extra_dirs = extra_dirs
        self.online = online
        self._lock = threading.RLock()
        self._tracks: list[LocalTrack] = []
        self._scanned_at = 0.0
        self._index_state = 0.0
        self._results: dict[tuple, tuple[str | None, str | None, str | None]] = {}
        self._images: "OrderedDict[str, Image.Image | None]" = OrderedDict()

    @staticmethod
    def _index_files() -> list[str]:
        return glob.glob(os.path.join(SPOTIFY_USERS_DIR, "*", "local-files.bnk"))

    def _current_index_state(self) -> float:
        times = [os.path.getmtime(f) for f in self._index_files() if os.path.exists(f)]
        return max(times, default=0.0)

    # foldery, w których Spotify ma pliki lokalne (wyciągnięte ze ścieżek w jego indeksie)
    def _spotify_dirs(self) -> set[str]:
        dirs = set()
        for index_file in self._index_files():
            try:
                with open(index_file, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            dirs.update(os.path.dirname(p) for p in _LOCAL_PATH_RE.findall(text))
        return {d for d in dirs if os.path.isdir(d)}

    def _audio_files(self) -> list[str]:
        files = set()
        for folder in self._spotify_dirs():
            try:
                files.update(os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(AUDIO_EXTENSIONS))
            except OSError:
                pass
        for folder in self.extra_dirs:
            for root, _, names in os.walk(folder):
                files.update(os.path.join(root, n) for n in names if n.lower().endswith(AUDIO_EXTENSIONS))
        return sorted(files)

    @staticmethod
    def _read_track(path: str) -> LocalTrack:
        title = artist = album = ""
        duration_ms = 0
        try:
            f = mutagen.File(path, easy=True)
            if f is not None:
                tags = f.tags or {}
                first = lambda key: str((tags.get(key) or [""])[0])
                title, artist, album = first("title"), first("artist"), first("album")
                duration_ms = int(getattr(f.info, "length", 0) * 1000)
        except Exception as e:
            logging.info(f"Nie udało się odczytać tagów {path}: {e}")
        titles = _filename_titles(path)
        if title:
            titles.add(_norm(title))
        return LocalTrack(path, titles, _artist_set(artist), _norm(album), duration_ms, artist, album)

    def scan(self):
        started = time.monotonic()
        state = self._current_index_state()
        tracks = [self._read_track(p) for p in self._audio_files()]
        with self._lock:
            self._tracks = tracks
            self._scanned_at = time.monotonic()
            self._index_state = state
        logging.info(f"Pliki lokalne: zindeksowano {len(tracks)} w {time.monotonic() - started:.1f} s")

    def _match(self, p: Playback) -> LocalTrack | None:
        title = _norm(p.title)
        artists = _artist_set(p.artist)
        album = _norm(p.album)
        best, best_score = None, -1
        for track in self._tracks:
            if title not in track.titles:
                continue
            score = 0
            if artists and track.artists & artists:
                score += 3
            if p.duration_ms and track.duration_ms and abs(p.duration_ms - track.duration_ms) <= 2500:
                score += 2
            if album and track.album == album:
                score += 1
            if score > best_score:
                best, best_score = track, score
        return best

    @staticmethod
    def _key(p: Playback) -> tuple:
        return (_norm(p.title), _norm(p.artist), p.duration_ms // 1000)

    # wynik z pamięci podręcznej albo None, jeśli utworu jeszcze nie sprawdzano
    def cached(self, p: Playback) -> tuple[str | None, str | None, str | None] | None:
        with self._lock:
            return self._results.get(self._key(p))

    # zwraca (klucz okładki, wykonawca z tagów, album z tagów); klucz to "file:<ścieżka>" albo URL z katalogu Spotify
    def resolve(self, p: Playback) -> tuple[str | None, str | None, str | None]:
        key = self._key(p)
        with self._lock:
            if key in self._results:
                return self._results[key]
            if not self._scanned_at:
                self.scan()
            track = self._match(p)
            stale = time.monotonic() - self._scanned_at > self.RESCAN_INTERVAL
            if (track is None or not os.path.exists(track.path)) and (stale or self._current_index_state() != self._index_state):
                self.scan()
                track = self._match(p)

        cover = artist = album = None
        cacheable = True
        if track is not None and os.path.exists(track.path):
            artist = track.tag_artist or None
            album = track.tag_album or None
            if self.load_image(track.path) is not None:
                cover = "file:" + track.path
        if cover is None and self.online:
            try:
                cover = self._search_spotify(p)
            except Exception as e:
                logging.info(f"Wyszukiwanie okładki w Spotify nie powiodło się: {e}")
                cacheable = False  # spróbujemy ponownie przy następnym odtworzeniu
        result = (cover, artist, album)
        if cacheable:
            with self._lock:
                self._results[key] = result
        return result

    # szukamy utworu w katalogu Spotify – akceptujemy tylko trafienie z tym samym tytułem i wykonawcą
    @staticmethod
    def _search_spotify(p: Playback) -> str | None:
        artists = _artist_set(p.artist)
        if not artists or not p.title:
            return None
        main_artist = re.split(r",|&|;", p.artist)[0].strip()
        results = sp.search(q=f"track:{p.title} artist:{main_artist}", type="track", limit=10)
        title = _norm(p.title)
        for item in (results.get("tracks") or {}).get("items") or []:
            if _norm(item.get("name", "")) != title:
                continue
            item_artists = set().union(*(_artist_set(a.get("name", "")) for a in item.get("artists", [])))
            if not artists & item_artists:
                continue
            images = (item.get("album") or {}).get("images") or []
            if images:
                return images[min(1, len(images) - 1)]["url"]
        return None

    # okładka z pliku (ID3 / FLAC / MP4 / Ogg), a gdy jej brak – cover.jpg / folder.jpg w tym samym folderze
    def load_image(self, path: str) -> Image.Image | None:
        with self._lock:
            if path in self._images:
                self._images.move_to_end(path)
                return self._images[path]
        image = None
        try:
            data = self._embedded_cover(path)
            if data:
                image = Image.open(BytesIO(data))
                image.load()
            else:
                image = self._folder_cover(os.path.dirname(path))
        except Exception as e:
            logging.info(f"Nie udało się odczytać okładki {path}: {e}")
            image = None
        with self._lock:
            self._images[path] = image
            while len(self._images) > 30:
                self._images.popitem(last=False)
        return image

    @staticmethod
    def _embedded_cover(path: str) -> bytes | None:
        f = mutagen.File(path)
        if f is None:
            return None
        pictures = getattr(f, "pictures", None)  # FLAC
        if pictures:
            front = [pic for pic in pictures if pic.type == 3]
            return (front or pictures)[0].data
        tags = f.tags
        if tags is None:
            return None
        if hasattr(tags, "getall"):  # ID3 (MP3, WAV)
            apics = tags.getall("APIC")
            if apics:
                front = [a for a in apics if a.type == 3]
                return (front or apics)[0].data
            return None
        if "covr" in tags and tags["covr"]:  # MP4 / M4A
            return bytes(tags["covr"][0])
        if "metadata_block_picture" in tags:  # Ogg Vorbis / Opus
            from mutagen.flac import Picture
            return Picture(base64.b64decode(tags["metadata_block_picture"][0])).data
        return None

    @staticmethod
    def _folder_cover(folder: str) -> Image.Image | None:
        try:
            names = os.listdir(folder)
        except OSError:
            return None
        for name in names:
            stem, ext = os.path.splitext(name.lower())
            if stem in FOLDER_COVER_NAMES and ext in (".jpg", ".jpeg", ".png"):
                image = Image.open(os.path.join(folder, name))
                image.load()
                return image
        return None


local_covers = LocalCoverResolver(LOCAL_MUSIC_DIRS, LOCAL_COVERS_ONLINE) if LOCAL_COVERS else None


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
WIDTH, HEIGHT = 400, 120
COVER_X, COVER_Y, COVER_SIZE = 48, 10, 100
TEXT_X = COVER_X + COVER_SIZE + 14      # 162
TEXT_W = WIDTH - TEXT_X - 14            # 224
STATE_Y = 93                            # środek linii z ikoną stanu i czasem
PROGRESS_Y = COVER_Y + COVER_SIZE - 3   # dół paska postępu równo z dołem okładki
# pionowy pasek głośności na całą wysokość: od góry okładki, liczba pod spodem kończy się równo z dołem okładki
VOL_X0, VOL_X1 = 20, 28
VOL_TOP, VOL_BOTTOM = COVER_Y + 3, 90
VOL_TEXT_Y = COVER_Y + COVER_SIZE - 7
LINE = "#3a3a3a"                        # tło paska postępu
TEXT_DIM = "#9a9a9a"
TEXT_STATE = "#c8c8c8"
PLACEHOLDER = "__placeholder__"
WARNING = "__warning__"


def _font_family(*candidates: str) -> str:
    families = set(tkfont.families())
    return next((f for f in candidates if f in families), "Segoe UI")


# rozmyta, przyciemniona okładka jako tło całej nakładki
def make_backdrop(image: Image.Image) -> Image.Image:
    backdrop = ImageOps.fit(image.convert("RGB"), (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(18))
    return ImageEnhance.Brightness(backdrop).enhance(0.28)


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
        # cache: klucz okładki -> (okładka, rozmyte tło lub None), ostatnie 30
        self._covers: "OrderedDict[str, tuple[ImageTk.PhotoImage, ImageTk.PhotoImage | None]]" = OrderedDict()
        self._current_images = None  # referencja chroni obrazy przed garbage collectorem
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

    # cała nakładka to jedno płótno – dzięki temu tekst może leżeć na rozmytym tle (etykiety Tk mają pełne tło)
    def _build_widgets(self):
        semibold = _font_family("Segoe UI Semibold")
        self.title_font = tkfont.Font(family=semibold, size=14)
        self.title_font_small = tkfont.Font(family=semibold, size=11)  # dla tytułów, które nie mieszczą się w 1 linii
        self.artist_font = tkfont.Font(family=semibold, size=9)
        self.small_font = tkfont.Font(family="Segoe UI", size=9)

        c = self.canvas = tk.Canvas(self.root, width=WIDTH, height=HEIGHT, bg=BG, highlightthickness=0, bd=0)
        c.place(x=0, y=0)
        self.backdrop_item = c.create_image(0, 0, anchor="nw")

        # głośność: szara szyna, wypełnienie w kolorze akcentu, biały znacznik i liczba pod spodem
        c.create_rectangle(VOL_X0, VOL_TOP, VOL_X1, VOL_BOTTOM, fill="#5a5a5a", width=0)
        self.vol_fill = c.create_rectangle(VOL_X0, VOL_BOTTOM, VOL_X1, VOL_BOTTOM, fill=OVERLAY_ACCENT, width=0)
        self.vol_thumb = c.create_rectangle(0, 0, 0, 0, fill="#f0f0f0", width=0, state="hidden")
        self.vol_text = c.create_text((VOL_X0 + VOL_X1) // 2, VOL_TEXT_Y, text="--", fill="white", font=self.small_font)

        self.cover_item = c.create_image(COVER_X, COVER_Y, anchor="nw")

        self.title_item = c.create_text(TEXT_X, 8, anchor="nw", text="", fill="white", font=self.title_font)
        self.artist_item = c.create_text(TEXT_X, 40, anchor="nw", text="", fill="white", font=self.artist_font)
        self.album_item = c.create_text(TEXT_X, 57, anchor="nw", text="", fill=TEXT_DIM, font=self.small_font)

        self.time_item = c.create_text(TEXT_X + 16, STATE_Y, anchor="w", text="", fill=TEXT_STATE, font=self.small_font)
        self.progress_track = c.create_rectangle(TEXT_X, PROGRESS_Y, TEXT_X + TEXT_W, PROGRESS_Y + 3,
                                                 fill=LINE, width=0, state="hidden")
        self.progress_fill = c.create_rectangle(TEXT_X, PROGRESS_Y, TEXT_X, PROGRESS_Y + 3,
                                                fill=OVERLAY_ACCENT, width=0, state="hidden")

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
        self._set_texts(playback.title, playback.artist, playback.album)
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

    # wynik wyszukania okładki pliku lokalnego (przychodzi chwilę po pokazaniu nakładki)
    def apply_local_result(self, track_id: str | None, cover: str | None, artist: str | None, album: str | None):
        p = self._playback
        if p is None or p.track_id != track_id:
            return  # w międzyczasie zmienił się utwór
        p.cover_url = cover
        if (artist and not p.artist) or (album and not p.album):
            p.artist = p.artist or artist or ""
            p.album = p.album or album or ""
            self._set_texts(p.title, p.artist, p.album)
        if cover:
            self._set_cover(cover)

    # ---- elementy nakładki ----

    def _set_volume(self, volume: int | None):
        c = self.canvas
        if volume is None:
            c.itemconfigure(self.vol_text, text="--")
            c.coords(self.vol_fill, VOL_X0, VOL_BOTTOM, VOL_X1, VOL_BOTTOM)
            c.itemconfigure(self.vol_thumb, state="hidden")
            return
        y = VOL_BOTTOM - (VOL_BOTTOM - VOL_TOP) * volume / 100
        c.coords(self.vol_fill, VOL_X0, y, VOL_X1, VOL_BOTTOM)
        c.coords(self.vol_thumb, VOL_X0 - 4, y - 3, VOL_X1 + 4, y + 3)
        c.itemconfigure(self.vol_thumb, state="normal")
        c.itemconfigure(self.vol_text, text=str(int(volume)))

    def _set_texts(self, title: str, subtitle: str, album: str = ""):
        c = self.canvas
        # duży, cienki tytuł jak w Windows; dłuższy – mniejszą czcionką w 2 liniach
        if self.title_font.measure(title) <= TEXT_W:
            font, text = self.title_font, title
        else:
            font, text = self.title_font_small, fit_lines(self.title_font_small, title, TEXT_W, max_lines=2)
        c.itemconfigure(self.title_item, text=text, font=font)
        y = 8 + (text.count("\n") + 1) * font.metrics("linespace") + 2
        c.coords(self.artist_item, TEXT_X, y)
        c.itemconfigure(self.artist_item, text=ellipsize(self.artist_font, subtitle, TEXT_W))

        # album pod wykonawcą – pomijamy pusty, taki sam jak tytuł (single) albo gdy nie zmieści się nad linią czasu
        album_y = y + self.artist_font.metrics("linespace") + 2
        fits = album_y + self.small_font.metrics("linespace") <= STATE_Y - 8
        if album and _norm(album) != _norm(title) and fits:
            c.coords(self.album_item, TEXT_X, album_y)
            c.itemconfigure(self.album_item, text=ellipsize(self.small_font, album, TEXT_W))
        else:
            c.itemconfigure(self.album_item, text="")

    def _update_progress(self):
        if self._tick_job:
            self.root.after_cancel(self._tick_job)
            self._tick_job = None
        c = self.canvas
        c.delete("state_icon")
        p = self._playback
        if p is None:
            c.itemconfigure(self.time_item, text="")
            c.itemconfigure(self.progress_track, state="hidden")
            c.itemconfigure(self.progress_fill, state="hidden")
            return
        self._draw_state_icon(p.is_playing)
        progress = p.progress_now()
        if p.duration_ms:
            c.itemconfigure(self.time_item, text=f"{format_ms(progress)} / {format_ms(p.duration_ms)}")
            c.coords(self.progress_fill, TEXT_X, PROGRESS_Y, TEXT_X + TEXT_W * progress / p.duration_ms, PROGRESS_Y + 3)
            c.itemconfigure(self.progress_track, state="normal")
            c.itemconfigure(self.progress_fill, state="normal")
        else:
            c.itemconfigure(self.time_item, text="")
            c.itemconfigure(self.progress_track, state="hidden")
            c.itemconfigure(self.progress_fill, state="hidden")
        # postęp liczymy lokalnie, bez dodatkowych zapytań do API
        if p.is_playing and self._visible:
            self._tick_job = self.root.after(500, self._update_progress)

    # pokazujemy stan, który jest teraz: trójkąt = gra, dwie kreski = pauza
    def _draw_state_icon(self, playing: bool):
        c = self.canvas
        x, y = TEXT_X + 1, STATE_Y - 5
        if playing:
            c.create_polygon(x, y, x, y + 10, x + 9, y + 5, fill=TEXT_STATE, outline="", tags="state_icon")
        else:
            c.create_rectangle(x, y, x + 3, y + 10, fill=TEXT_STATE, width=0, tags="state_icon")
            c.create_rectangle(x + 5, y, x + 8, y + 10, fill=TEXT_STATE, width=0, tags="state_icon")

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
            if url.startswith("file:"):
                image = local_covers.load_image(url[5:]) if local_covers else None
                if image is None:
                    raise ValueError("brak okładki w pliku")
            else:
                response = self.session.get(url, timeout=5)
                response.raise_for_status()
                image = Image.open(BytesIO(response.content))
            image = image.convert("RGB")
            backdrop = make_backdrop(image) if OVERLAY_BLUR else None
            # przycięcie do kwadratu (okładki z plików nie zawsze są kwadratowe)
            cover = ImageOps.fit(image, (COVER_SIZE, COVER_SIZE), Image.Resampling.LANCZOS)
        except Exception as e:
            logging.error(f"Błąd pobierania okładki ({url}): {e}")
            cover = backdrop = None
        ui(lambda: self._cover_downloaded(url, cover, backdrop))

    def _cover_downloaded(self, url: str, cover: Image.Image | None, backdrop: Image.Image | None):
        if cover is None:
            if self._wanted_cover == url:
                self._set_cover(PLACEHOLDER)
            return
        self._store_cover(url, cover, backdrop)
        if self._wanted_cover == url:  # ignoruj, jeśli w międzyczasie zmienił się utwór
            self._apply_cover(url)

    def _store_cover(self, key: str, cover: Image.Image, backdrop: Image.Image | None = None):
        self._covers[key] = (ImageTk.PhotoImage(cover), ImageTk.PhotoImage(backdrop) if backdrop else None)
        while len(self._covers) > 30:
            self._covers.popitem(last=False)

    def _apply_cover(self, key: str):
        cover, backdrop = self._covers[key]
        self.canvas.itemconfigure(self.cover_item, image=cover)
        self.canvas.itemconfigure(self.backdrop_item, image=backdrop or "")
        self._current_images = (cover, backdrop)

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

    # plik lokalny: okładka z pamięci podręcznej od razu, a jeśli utworu jeszcze nie sprawdzano – w tle
    lookup = False
    if local_covers and playback.is_local and not playback.cover_url:
        cached = local_covers.cached(playback)
        if cached is None:
            lookup = True
        else:
            cover, artist, album = cached
            playback.cover_url = cover
            playback.artist = playback.artist or artist or ""
            playback.album = playback.album or album or ""

    ui(lambda: overlay.show_playback(playback))
    if lookup:
        threading.Thread(target=_resolve_local_cover, args=(playback,), daemon=True).start()


def _resolve_local_cover(playback: Playback):
    try:
        cover, artist, album = local_covers.resolve(playback)
    except Exception as e:
        logging.error(f"Błąd wyszukiwania okładki pliku lokalnego: {e}")
        return
    ui(lambda: overlay.apply_local_result(playback.track_id, cover, artist, album))


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
    if local_covers:
        # indeks plików lokalnych budujemy od razu w tle, żeby pierwsza okładka pojawiła się bez czekania
        threading.Thread(target=local_covers.scan, daemon=True, name="local-scan").start()

    overlay.start()
    keyboard.unhook_all()
    tray.stop()
    logging.shutdown()


if __name__ == "__main__":
    main()
