# spotify volume control & overlay
basic app made in python for controlling in app volume with global shortcuts using spotify api, made  for windows.

## features
* volume control: shortcuts `Ctrl` + `Alt` + `Arrow Up/Down` for changing volume in app by 5%, add `Shift` for 1% steps. the overlay reacts instantly, fast/held presses are merged into a single API request.
* overlay: styled after the Windows media flyout – thin volume bar matching the progress bar, album cover, large title, artist, album (hidden for singles where it matches the title), play/pause state with track progress, blurred cover as the background; fades in/out and autohides after 3 seconds. it's display-only, clicks pass through it
* media keys: overlay shows up after play/pause/next/previous
* podcasts: episodes are shown with the show name and cover
* local files: the cover is read from the file itself (MP3, FLAC, M4A, Ogg) or from `cover.jpg` / `folder.jpg` next to it; files are found through Spotify's own local files index, so no setup is needed. if the file has no cover, the track is looked up in the Spotify catalog (same title and artist)
* clear messages instead of silent failures: no active device, no internet connection, Spotify Premium required, login required
* tray icon: connection status (green / red / grey), show overlay, log in again, start with Windows, open log, quit
* automatic re-login in the browser when the Spotify token expires or gets revoked
* only one instance can run at a time

## requirements
* windows 11 (maybe 10)
* python, preferably 3.13 (or newer)
* spotify API (app/credentials)
* spotify premium (required by the API for changing volume)

## installation

1. 
    ```bash
    git clone [https://github.com/mateuszwator/volume-control.git](https://github.com/mateuszwator/volume-control.git)
    cd volume-control
    ```

2. 
    ```bash
    pip install spotipy keyboard pillow requests python-dotenv pystray mutagen
    ```

3.  **configure spotify API:**
    * https://developer.spotify.com/dashboard/.
    * create new app
    * set the redirect URL: `http://127.0.0.1:8888/callback`
    * make a `.env` file and fill it with your credentials:
        ```env
        SPOTIPY_CLIENT_ID=
        SPOTIPY_CLIENT_SECRET=
        SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
        ```

4. **first run - authorization**
    on the first launch a browser window will open asking to authorize the app.
    if the authorization expires later, the browser opens again automatically (or use *log in again* in the tray menu).

## settings (optional, in `.env`)
| variable | default | description |
|---|---|---|
| `HOTKEY_VOLUME_UP` | `ctrl+alt+up` | shortcut for volume up |
| `HOTKEY_VOLUME_DOWN` | `ctrl+alt+down` | shortcut for volume down |
| `VOLUME_STEP` | `5` | volume change per press (1–50) |
| `HOTKEY_VOLUME_UP_FINE` | `ctrl+alt+shift+up` | shortcut for fine volume up |
| `HOTKEY_VOLUME_DOWN_FINE` | `ctrl+alt+shift+down` | shortcut for fine volume down |
| `VOLUME_STEP_FINE` | `1` | fine volume change per press (1–50) |
| `OVERLAY_POSITION` | `top-left` | `top-left`, `top-right`, `bottom-left` or `bottom-right` |
| `OVERLAY_MARGIN` | `50` | distance from the screen edge in px |
| `OVERLAY_TIMEOUT` | `3` | seconds before the overlay hides |
| `OVERLAY_OPACITY` | `0.96` | overlay opacity (0.2–0.99) |
| `OVERLAY_FADE_MS` | `150` | fade in/out duration in ms (`0` disables it) |
| `OVERLAY_ACCENT` | `#0078d4` | color of the volume and progress bars: `#rrggbb` or `system` (Windows accent color) |
| `OVERLAY_BLUR` | `true` | blurred album cover as the overlay background |
| `LOCAL_COVERS` | `true` | show covers for local files |
| `LOCAL_COVERS_ONLINE` | `true` | if a local file has no cover, look the track up in the Spotify catalog |
| `LOCAL_MUSIC_DIRS` | *(empty)* | extra music folders searched recursively, separated with `;` (folders added in Spotify are found automatically) |

## for windowless running in background:
1.  make a start.bat file with:
    ```batch
    @echo off
    start "" pythonw3.13 "%~dp0spotify_overlay.py"
    ```
    (replace `pythonw3.13` with the full path to `pythonw.exe` if it's not on your PATH)

2.  save the file in the project directory

3.  run it

to start the app with Windows, enable *start with Windows* in the tray menu.

> check `debug_overlay.log` for error details (also available from the tray menu).
