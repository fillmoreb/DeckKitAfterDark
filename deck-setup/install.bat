@echo off
rem Installs DeckLink's program side on the Steam Deck from a Windows PC, over SSH (Windows 10 and 11
rem have ssh and tar built in). Same as install.sh: copies this folder over and runs install-on-deck.sh
rem there as the deck user. On the Deck itself, run install-on-deck.sh instead.
rem
rem First, on the Deck (Desktop Mode, Konsole): set a password with `passwd` and turn SSH on with
rem `sudo systemctl enable --now sshd`. Then here:
rem     install.bat deck@steamdeck.local
rem (or the Deck's IP address: deck@192.168.x.x). ssh asks for the Deck's password.
setlocal
cd /d "%~dp0"
set "DECK=%~1"
if "%DECK%"=="" if exist "..\deploy.local" (
    for /f "usebackq tokens=1,* delims==" %%a in ("..\deploy.local") do if "%%a"=="DECK" set "DECK=%%b"
)
if "%DECK%"=="" (
    echo Give the Deck's address: install.bat deck@steamdeck.local
    exit /b 1
)
tar --exclude __pycache__ -cf - decklink decktation-hook.sh install-on-deck.sh | ssh %DECK% "d=$(mktemp -d) && chmod 755 \"$d\" && cd \"$d\" && tar xf - && if [ \"$(id -un)\" = deck ]; then bash install-on-deck.sh; else sudo -u deck bash install-on-deck.sh; fi; cd / && rm -rf \"$d\""
if errorlevel 1 (
    echo Install failed.
    exit /b 1
)
endlocal
