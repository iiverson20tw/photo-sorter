@echo off
cd /d "%~dp0"
python server.py 8091 > "%~dp0_hub_start.log" 2>&1
