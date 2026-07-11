@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting photo-sorter (shared album) server + Cloudflare tunnel...
start "photo-server" /min python server.py 8090
start "photo-tunnel" /min cmd /c "F:\AI\cloudflared\cloudflared.exe tunnel --url http://127.0.0.1:8090 > cf.log 2>&1"
echo Waiting for public URL (about 10s)...
timeout /t 11 /nobreak >nul
echo.
echo ================= SHARE THIS URL =================
findstr /R /C:"trycloudflare.com" cf.log
echo =================================================
echo.
echo Server and tunnel keep running in the background.
echo (URL changes each time you restart. Re-run this to get the new one.)
pause
