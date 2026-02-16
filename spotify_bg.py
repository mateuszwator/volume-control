import spotipy
from spotipy.oauth2 import SpotifyOAuth
import keyboard
import time
import os
from dotenv import load_dotenv
import logging

# logowanie do pliku, zeby nie smiecic uzytkownikowi
logging.basicConfig(filename='error_log.txt', level=logging.ERROR, 
                    format='%(asctime)s %(message)s')

# ustawienie katalogu roboczego na ten, w ktorym znajduje sie skrypt, zeby plik .env byl zawsze znajdowany bez wzgledu na to, skad uruchamiamy program
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# wczytanie danych z .env
load_dotenv()

CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI')

scope = "user-read-playback-state,user-modify-playback-state"

# proba logowania - jesli sie nie uda, zapisujemy blad i wychodzimy (program i tak nic nie zrobi bez polaczenia)
try:
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=CLIENT_ID,
                                                   client_secret=CLIENT_SECRET,
                                                   redirect_uri=REDIRECT_URI,
                                                   scope=scope))
except Exception as e:
    logging.error(f"Blad logowania: {e}")

def change_volume(step):
    try:
        # aktualna glosnosc jest zwracana tylko gdy cos gra, wiec najpierw sprawdzamy co jest aktualnie odtwarzane
        current = sp.current_playback()
        device_id = None
        volume_now = None

        if current and current['device']:
            device_id = current['device']['id']
            volume_now = current['device']['volume_percent']
        
        # jesli nic nie gra, API nie zwroci informacji o glosnosci, ale nadal mozemy ja zmienic - wtedy musimy znalezc aktywne urzadzenie
        else:
            devices = sp.devices()
            if devices['devices']:
                # najpierw szukamy komputera, bo to najczestszy przypadek uzytkownika, ale jesli nie znajdziemy, wezmiemy pierwsze co jest
                for d in devices['devices']:
                    if d['type'] == 'Computer':
                        device_id = d['id']
                        volume_now = d['volume_percent']
                        break
                
                # jesli nie znalezlismy komputera, ale mamy inne urzadzenia, bierzemy pierwsze z nich
                if not device_id and devices['devices']:
                    device_id = devices['devices'][0]['id']
                    volume_now = devices['devices'][0]['volume_percent']

        # jesli nadal nie mamy urzadzenia, to znaczy ze Spotify jest zamkniete - wtedy nie ma co zmieniac glosnosci
        if not device_id:
            return # nie robimy nic, bo nie ma gdzie zmieniac glosnosci

        # jesli nie mamy informacji o aktualnej glosnosci, ustawiamy ja na 50% jako bezpieczna wartosc domyslna
        if volume_now is None: volume_now = 50 
        
        new_vol = volume_now + step
        if new_vol > 100: new_vol = 100
        if new_vol < 0: new_vol = 0

        # zmieniamy glosnosc na nowe ustawienie
        sp.volume(int(new_vol), device_id=device_id)

    except Exception as e:
        # jesli cokolwiek poszlo nie tak, zapisujemy blad do logu, ale nie przerywamy programu, zeby uzytkownik mogl dalej korzystac z klawiatury
        logging.error(str(e))

# ustawienie hotkeyow - ctrl+alt+up do zwiekszania glosnosci, ctrl+alt+down do zmniejszania
try:
    keyboard.add_hotkey('ctrl+alt+up', lambda: change_volume(5))
    keyboard.add_hotkey('ctrl+alt+down', lambda: change_volume(-5))
    keyboard.wait()
except Exception as e:
    logging.error(f"Blad klawiatury: {e}")