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

# --- FIX NA ŚCIEŻKI ---
os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

# --- KONFIGURACJA API ---
CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI')

# Tlumienie bledow
sys.stderr = open('debug_overlay.log', 'w') 

scope = "user-read-playback-state,user-modify-playback-state"

try:
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=CLIENT_ID,
                                                   client_secret=CLIENT_SECRET,
                                                   redirect_uri=REDIRECT_URI,
                                                   scope=scope))
except Exception as e:
    with open("debug_overlay.log", "a") as f: f.write(str(e))

# --- GUI (Wyglad) ---
class VolumeOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True) 
        self.root.attributes("-topmost", True) 
        self.root.attributes("-alpha", 0.95) 
        self.root.configure(bg="#1f1f1f") 
        self.root.geometry(f"350x120+50+50") 

        self.hide_timer = None

        # Elementy
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
        # Aktualizacja tekstow
        self.title_label.config(text=title)
        self.artist_label.config(text=artist)
        
        # Pasek glosnosci
        if volume is not None:
            bar_width = int(volume * 2) 
            self.vol_canvas.coords(self.vol_bar, 0, 0, bar_width, 6)

        # Pobieranie okladki
        if cover_url and cover_url != self.current_cover_url:
            self.current_cover_url = cover_url
            threading.Thread(target=self.download_image, args=(cover_url,), daemon=True).start()

        self.root.deiconify()
        
        # Restart timera (3 sekundy)
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

# --- MONITOR ZMIAN (Auto-wykrywanie piosenki) ---
def monitor_loop():
    last_track_id = None
    
    while True:
        try:
            current = sp.current_playback()
            
            if current and current['item']:
                current_id = current['item']['id']
                
                # CZY TO NOWA PIOSENKA?
                if current_id != last_track_id:
                    last_track_id = current_id
                    
                    # Pobierz dane
                    track_name = current['item']['name']
                    artists = ", ".join([a['name'] for a in current['item']['artists']])
                    vol = current['device']['volume_percent']
                    cover = current['item']['album']['images'][0]['url'] if current['item']['album']['images'] else None
                    
                    # Pokaz overlay
                    overlay.root.after(0, lambda: overlay.update_info(track_name, artists, vol, cover))
            
            # Odczekaj chwile przed kolejnym sprawdzeniem (oszczedzanie procesora)
            time.sleep(1.5) 
            
        except Exception as e:
            # Ignoruj bledy polaczenia (np. brak neta przez chwile)
            time.sleep(5)

# Uruchom monitor w tle
threading.Thread(target=monitor_loop, daemon=True).start()


# --- STEROWANIE GŁOŚNOŚCIĄ (Twoje skróty) ---
def change_volume(step):
    threading.Thread(target=worker_volume, args=(step,), daemon=True).start()

def worker_volume(step):
    try:
        current = sp.current_playback()
        if current and current['device']:
            vol = current['device']['volume_percent']
            new_vol = max(0, min(100, vol + step))
            sp.volume(int(new_vol))
            
            # Przy zmianie glosnosci tez odswiezamy GUI
            track = current['item']['name']
            art = ", ".join([a['name'] for a in current['item']['artists']])
            cover = current['item']['album']['images'][0]['url'] if current['item']['album']['images'] else None
            
            overlay.root.after(0, lambda: overlay.update_info(track, art, new_vol, cover))
    except:
        pass

# Skróty tylko do głośności (reszte robi Monitor)
keyboard.add_hotkey('ctrl+alt+up', lambda: change_volume(5))
keyboard.add_hotkey('ctrl+alt+down', lambda: change_volume(-5))

# Start
overlay.start()