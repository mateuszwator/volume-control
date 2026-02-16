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

# pobranie danych autoryzacyjnych z .env
CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI')

# przekierowanie stderr do pliku, aby debugować ewentualne błędy
sys.stderr = open('debug_overlay.log', 'w') 

scope = "user-read-playback-state,user-modify-playback-state"

try:
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=CLIENT_ID,
                                                   client_secret=CLIENT_SECRET,
                                                   redirect_uri=REDIRECT_URI,
                                                   scope=scope))
except Exception as e:
    with open("debug_overlay.log", "a") as f: f.write(str(e))

# główna klasa odpowiedzialna za nakładkę
class VolumeOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True) 
        self.root.attributes("-topmost", True) 
        
        # ustawienie przezroczystości (0.0 - całkowicie przezroczyste, 1.0 - nieprzezroczyste)
        self.root.attributes("-alpha", 0.9) 
        
        self.root.configure(bg="#1f1f1f") 
        self.root.geometry(f"350x120+50+50") 

        # ustawienia okna na "przenikające" (kliknięcia przechodzą przez nie)
        self.root.update_idletasks() # potrzebne do poprawnego pobrania ID okna
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        
        # definicje stałych dla stylów okna
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        
        # pobranie aktualnego stylu i dodanie flag dla nakładki
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)

        self.hide_timer = None

        # elementy GUI
        self.cover_label = tk.Label(self.root, bg="#1f1f1f")
        self.cover_label.place(x=10, y=10, width=100, height=100)

        self.title_label = tk.Label(self.root, text="Spotify", font=("Segoe UI", 12, "bold"), 
                                    fg="white", bg="#1f1f1f", anchor="w")
        self.title_label.place(x=120, y=15, width=220)

        self.artist_label = tk.Label(self.root, text="...", font=("Segoe UI", 10), 
                                     fg="#b3b3b3", bg="#1f1f1f", anchor="w")
        self.artist_label.place(x=120, y=40, width=220)

        self.vol_canvas = tk.Canvas(self.root, bg="#404040", highlightthickness=0)
        self.vol_canvas.place(x=120, y=80, width=200, height=6)
        self.vol_bar = self.vol_canvas.create_rectangle(0, 0, 0, 6, fill="#1db954", width=0)

        self.root.withdraw()
        self.current_cover_url = ""

    def update_info(self, title, artist, volume, cover_url):
        # aktualizacja tekstow
        self.title_label.config(text=title)
        self.artist_label.config(text=artist)
        
        # aktualizacja paska glosnosci
        if volume is not None:
            bar_width = int(volume * 2) 
            self.vol_canvas.coords(self.vol_bar, 0, 0, bar_width, 6)

        # pobierz i ustaw okładkę tylko jeśli się zmieniła
        if cover_url and cover_url != self.current_cover_url:
            self.current_cover_url = cover_url
            threading.Thread(target=self.download_image, args=(cover_url,), daemon=True).start()

        self.root.deiconify()
        
        # automatyczne ukrywanie po 3 sekundach
        if self.hide_timer:
            self.root.after_cancel(self.hide_timer)
        self.hide_timer = self.root.after(3000, self.root.withdraw)

    def download_image(self, url):
        try:
            response = requests.get(url)
            img_data = BytesIO(response.content)
            pil_image = Image.open(img_data)
            pil_image = pil_image.resize((100, 100), Image.Resampling.LANCZOS)
            tk_image = ImageTk.PhotoImage(pil_image)
            self.root.after(0, lambda: self.set_image(tk_image))
        except Exception:
            pass

    def set_image(self, img):
        self.cover_label.config(image=img)
        self.cover_label.image = img 

    def start(self):
        self.root.mainloop()

overlay = VolumeOverlay()

# funkcja odswiezajaca nakladke - pobiera aktualny stan i aktualizuje GUI
def refresh_overlay(delay=0.1):
    time.sleep(delay) 
    try:
        current = sp.current_playback()
        if current and current['item']:
            track = current['item']['name']
            art = ", ".join([a['name'] for a in current['item']['artists']])
            vol = current['device']['volume_percent']
            cover = current['item']['album']['images'][0]['url'] if current['item']['album']['images'] else None
            
            # Zleć aktualizację GUI
            overlay.root.after(0, lambda: overlay.update_info(track, art, vol, cover))
    except:
        pass

# sterowanie glosnoscia (skroty klawiszowe)
def change_volume(step):
    threading.Thread(target=worker_volume, args=(step,), daemon=True).start()

def worker_volume(step):
    try:
        current = sp.current_playback()
        if current and current['device']:
            vol = current['device']['volume_percent']
            new_vol = max(0, min(100, vol + step))
            sp.volume(int(new_vol))
            
            # przy zmianie glosnosci tez odswiezamy GUI
            track = current['item']['name']
            art = ", ".join([a['name'] for a in current['item']['artists']])
            cover = current['item']['album']['images'][0]['url'] if current['item']['album']['images'] else None
            
            overlay.root.after(0, lambda: overlay.update_info(track, art, new_vol, cover))
    except:
        pass

# rejestracja skrótów klawiszowych
keyboard.add_hotkey('ctrl+alt+up', lambda: change_volume(5))
keyboard.add_hotkey('ctrl+alt+down', lambda: change_volume(-5))

# funkcja wywoływana przy naciśnięciu klawiszy multimedialnych
def on_any_key(event):
    if event.event_type == 'down': # reagujemy tylko na naciśnięcia, nie na zwolnienia
        name = event.name.lower()
        
        # definiujemy słowa kluczowe, które mogą występować w nazwach klawiszy multimedialnych
        keywords = ['track', 'media', 'play', 'pause', 'next', 'previous']
        
        # jeśli nazwa klawisza zawiera któreś ze słów kluczowych, odświeżamy nakładkę
        if any(word in name for word in keywords):
            # odświeżamy nakładkę w osobnym wątku, żeby nie blokować głównego wątku GUI
            threading.Thread(target=refresh_overlay, args=(0.5,), daemon=True).start()

# rejestracja globalnego nasłuchiwania klawiszy
keyboard.hook(on_any_key)

# start GUI
overlay.start()