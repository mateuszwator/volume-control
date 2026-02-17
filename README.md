# spotify volume control & overlay
basic app made in python for controlling in app volume with global shortcuts using spotify api, made  for windows.

## features
* volume control: shortcuts `Ctrl` + `Alt` + `Arrow Up/Down` for changing volume in app.
* overlay: shows album cover, title, artist and volume bar, default autohide after 3 seconds

## requirements
* windows 11 (maybe 10)
* python, preferably 3.13 (or newer)
* spotify API (app/credentials)

## installation

1. 
    ```bash
    git clone [https://github.com/mateuszwator/volume-control.git](https://github.com/mateuszwator/volume-control.git)
    cd volume-control
    ```

2. 
    ```bash
    pip install spotipy keyboard pillow requests python-dotenv
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

## for windowless running in background:
1.  make a start.bat file with:
    ```batch
    @echo off
    start "" "C:\path\to\pythonw.exe" "spotify_overlay.py"
    ```

2.  save the file in the project directory

3.  run it

> check debug_overlay.log or error.log for error details.