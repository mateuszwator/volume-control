import logging
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import keyboard
import os
import sys
import tkinter as tk
from PIL import Image, ImageTk
import requests
from io import BytesIO
import threading
import time
from dotenv import load_dotenv
import ctypes

# ustawienie katalogu roboczego na ten, w którym znajduje się skrypt, aby poprawnie ładować .env
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

# konfiguracja logowania do pliku, aby rejestrować błędy i ułatwić debugowanie, jeśli coś pójdzie nie tak z autoryzacją lub API Spotify
logging.basicConfig(
    filename="debug_overlay.log",
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# pobranie danych autoryzacyjnych z .env
CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI')

if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI]):
    logging.critical("no env vars set for Spotify API (SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI)")
    sys.exit("error: .env file is missing required Spotify API environment variables.")

scope = "user-read-playback-state,user-modify-playback-state"

try:
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=CLIENT_ID,
                                                   client_secret=CLIENT_SECRET,
                                                   redirect_uri=REDIRECT_URI,
                                                   scope=scope))
except Exception as e:
    logging.critical(f"Could not initialize Spotify client: {e}")
    sys.exit(f"Spotify authorization error: {e}")

# funkcja pomocnicza do pobierania URL okładki albumu z danych utworu, z obsługą sytuacji, gdy okładka może być niedostępna
def get_cover_url(item: dict) -> str | None:
    # API Spotify może zwrócić różne formaty danych, więc bezpiecznie sprawdzamy, czy klucze istnieją, zanim spróbujemy uzyskać URL okładki
    images = item.get('album', {}).get('images', [])
    if not images:
        return None
    # zazwyczaj Spotify zwraca trzy rozmiary okładek (640x640, 300x300, 64x64), więc wybieramy średni rozmiar (300x300), 
    # który jest wystarczająco duży do nakładki, ale nie za duży, żeby nie obciążać pobierania
    idx = min(1, len(images) - 1)
    return images[idx]['url']

# główna klasa odpowiedzialna za nakładkę
class VolumeOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True) 
        self.root.attributes("-topmost", True) 
        
        # ustawienie przezroczystości (0.0 - całkowicie przezroczyste, 1.0 - nieprzezroczyste)
        self.root.attributes("-alpha", 0.9) 
        
        self.root.configure(bg="#1f1f1f") 
        self.root.geometry(f"380x120+50+50") 

        # ustawienia okna na "przenikające" (kliknięcia przechodzą przez nie)
        self.root.update() # musimy zaktualizować okno, aby mieć poprawne ID, zanim możemy zmienić jego style
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        
        # definicje stałych dla stylów okna
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        
        # pobranie aktualnego stylu i dodanie flag dla nakładki
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)

        self.hide_timer = None

        # sesja do pobierania okładek albumów, aby uniknąć tworzenia nowych połączeń przy każdym pobieraniu
        self.session = requests.Session()

        
        self.current_cover_url = ""

        self._build_widgets()
        self.root.withdraw()

    # metoda do tworzenia i układania widgetów w nakładce
    def _build_widgets(self):
        # ustawienia dla paska głośności
        self.vol_canvas_height = 80

        # pasek głośności (pionowy)
        self.vol_canvas = tk.Canvas(self.root, bg="#404040", highlightthickness=0)
        self.vol_canvas.place(x=15, y=15, width=12, height=self.vol_canvas_height)

        self.vol_bar = self.vol_canvas.create_rectangle(
            0, self.vol_canvas_height, 12, self.vol_canvas_height,
            fill="#cccccc", width=0
        )

        # etykieta z wartością procentową głośności — pod paskiem
        self.vol_number_label = tk.Label(
            self.root, text="--",
            font=("Segoe UI", 9), fg="white", bg="#1f1f1f", anchor="center"
        )
        self.vol_number_label.place(x=0, y=98, width=42)

        # okładka albumu
        self.cover_label = tk.Label(self.root, bg="#1f1f1f")
        self.cover_label.place(x=60, y=10, width=100, height=100)

        # tytuł i artysta
        self.title_label = tk.Label(
            self.root, text="Spotify",
            font=("Segoe UI", 11, "bold"), fg="white", bg="#1f1f1f", anchor="nw"
        )
        self.title_label.place(x=170, y=10, width=190)

        self.artist_label = tk.Label(
            self.root, text="...",
            font=("Segoe UI", 9), fg="#b3b3b3", bg="#1f1f1f", anchor="nw"
        )
        self.artist_label.place(x=170, y=35, width=190)

    # metoda do aktualizacji informacji wyświetlanych na nakładce, która jest wywoływana z głównego wątku GUI, aby zapewnić płynne aktualizacje bez blokowania interfejsu
    def update_info(self, title, artist, volume, cover_url):
        # aktualizacja tekstow
        self.title_label.config(text=title)
        self.artist_label.config(text=artist)
        
        # aktualizacja paska glosnosci
        if volume is not None:
            # aktualizacja liczby procentowej
            self.vol_number_label.config(text=f"{int(volume)}%")
            
            # aktualizacja paska graficznego
            bar_height_px = int((volume / 100) * self.vol_canvas_height)
            top_y = self.vol_canvas_height - bar_height_px
            self.vol_canvas.coords(self.vol_bar, 0, top_y, 14, self.vol_canvas_height)

        # pobierz i ustaw okładkę tylko jeśli się zmieniła
        if cover_url and cover_url != self.current_cover_url:
            self.current_cover_url = cover_url
            threading.Thread(target=self._download_image, args=(cover_url,), daemon=True).start()

        self.root.deiconify()
        
        # automatyczne ukrywanie po 3 sekundach
        if self.hide_timer:
            self.root.after_cancel(self.hide_timer)
        self.hide_timer = self.root.after(3000, self.root.withdraw)

    # metoda do pobierania obrazu z URL i aktualizacji nakładki, która jest uruchamiana w osobnym wątku, aby nie blokować głównego wątku GUI podczas pobierania i przetwarzania obrazu
    def _download_image(self, url):
        try:
            response = self.session.get(url, timeout=5)
            img_data = BytesIO(response.content)
            pil_image = Image.open(img_data)
            # zmniejszenie obrazu do 100x100px, aby pasował do nakładki
            pil_image = pil_image.resize((100, 100), Image.Resampling.LANCZOS)
            tk_image = ImageTk.PhotoImage(pil_image)
            self.root.after(0, lambda: self._set_image(tk_image))
        except Exception as e:
            logging.error(f"Error downloading cover image ({url}): {e}")

    # metoda do ustawiania obrazu w nakładce, która jest wywoływana z głównego wątku GUI po pobraniu i przetworzeniu obrazu, aby bezpiecznie zaktualizować widget z obrazem
    def _set_image(self, img):
        self.cover_label.config(image=img)
        # ważne: musimy przechowywać referencję do obrazu, aby zapobiec jego usunięciu przez garbage collector, co spowodowałoby zniknięcie obrazu z nakładki
        self.cover_label.image = img 

    # metoda do uruchamiania głównej pętli GUI, która jest wywoływana na końcu skryptu, aby rozpocząć wyświetlanie nakładki i reagowanie na zdarzenia
    def start(self):
        self.root.mainloop()

overlay = VolumeOverlay()

# zmienne do zarządzania odświeżaniem nakładki, aby uniknąć nadmiernego odświeżania przy szybkim naciskaniu klawiszy multimedialnych
_refresh_timer: threading.Timer | None = None
_refresh_lock = threading.Lock()

# funkcja do odświeżania nakładki, która jest wywoływana z wątku nasłuchującego klawisze multimedialne, aby pobrać aktualne informacje o odtwarzaniu i zaktualizować nakładkę
def _debounced_refresh(delay: float):
    # używamy blokady, aby zapewnić, że tylko jeden timer odświeżania jest aktywny w danym momencie, co zapobiega nadmiernemu odświeżaniu przy szybkim naciskaniu klawiszy
    global _refresh_timer
    with _refresh_lock:
        if _refresh_timer is not None:
            _refresh_timer.cancel()
        _refresh_timer = threading.Timer(delay, _fetch_and_update)
        _refresh_timer.daemon = True
        _refresh_timer.start()

# funkcja do bezpośredniego odświeżania nakładki, która jest wywoływana z wątku nasłuchującego klawisze multimedialne, aby natychmiast pobrać aktualne informacje o odtwarzaniu i zaktualizować nakładkę, bez opóźnienia debouncingu
def _fetch_and_update():
    try:
        current = sp.current_playback()
        if current and current.get('item'):
            item = current['item']
            track = item['name']
            artist = ", ".join(a['name'] for a in item['artists'])
            vol = current['device']['volume_percent']
            cover = get_cover_url(item)
            overlay.root.after(0, lambda: overlay.update_info(track, artist, vol, cover))
    except Exception as e:
        logging.error(f"Error fetching and updating overlay: {e}")

# sterowanie glosnoscia (skroty klawiszowe)
def change_volume(step: int):
    threading.Thread(target=_worker_volume, args=(step,), daemon=True).start()

# funkcja robocza do zmiany głośności, która jest uruchamiana w osobnym wątku, aby nie blokować głównego wątku GUI podczas komunikacji z API Spotify i aktualizacji nakładki
def _worker_volume(step: int):
    try:
        current = sp.current_playback()
        if not (current and current.get('device')):
            return

        vol = current['device']['volume_percent']
        new_vol = max(0, min(100, vol + step))
        sp.volume(int(new_vol))

        item = current.get('item')
        if item:
            track = item['name']
            artist = ", ".join(a['name'] for a in item['artists'])
            cover = get_cover_url(item)
            overlay.root.after(0, lambda: overlay.update_info(track, artist, new_vol, cover))
    except Exception as e:
        logging.error(f"Error changing volume: {e}")

# rejestracja skrótów klawiszowych
keyboard.add_hotkey('ctrl+alt+up', lambda: change_volume(5))
keyboard.add_hotkey('ctrl+alt+down', lambda: change_volume(-5))

# definiujemy zbiór słów kluczowych, które mogą występować w nazwach klawiszy multimedialnych, aby łatwo sprawdzać, czy naciśnięty klawisz jest klawiszem multimedialnym
MEDIA_KEY_KEYWORDS = frozenset(['track', 'media', 'play', 'pause', 'next', 'previous'])

# funkcja wywoływana przy naciśnięciu klawiszy multimedialnych
def on_any_key(event: keyboard.KeyboardEvent):
    # reagujemy tylko na naciśnięcia (nie zwolnienia)
    if event.event_type != 'down':
        return
    name = event.name.lower()
    # Spotify potrzebuje chwili na propagację zmiany stanu — stąd delay 0.5s
    if any(word in name for word in MEDIA_KEY_KEYWORDS):
        _debounced_refresh(delay=0.5)

# rejestracja globalnego nasłuchiwania klawiszy
keyboard.hook(on_any_key)

# start GUI
overlay.start()