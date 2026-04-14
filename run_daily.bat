@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python src\main.py >> logs\run_%date:~0,4%%date:~5,2%%date:~8,2%.log 2>&1
