# spotify volume control n overlay

basic app made in python to control in app volume with global shortcuts using spotify api, made  for windows.

## functions
* volume control: shortcuts `Ctrl` + `Alt` + `Arrow Up/Down` for changing volume in app.
* overlay: shows album cover, title, artist and volume bar

## requirements
* windows 11 (maybe 10)
* python, preferably 3.13 (or newer)
* spotify API

## instalation

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
    * make a redirect URL: `http://127.0.0.1:8888/callback`
    * make a `.env` file and paste data into it:
        ```env
        SPOTIPY_CLIENT_ID=
        SPOTIPY_CLIENT_SECRET=
        SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
        ```


## for windowless running in background:

1.  make a start.bat file with:

    ```batch
    @echo off
    start "" "C:\Users\your_user\AppData\Local\Microsoft\WindowsApps\pythonw3.13.exe" "spotify_overlay.py"
    ```

2.  save the file in the project directory
3.  run it
