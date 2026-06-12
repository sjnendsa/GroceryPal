@echo off
title Grocery Pal
cd /d "%~dp0"
set GROCERY_PAL_OPEN=1
set PYTHONIOENCODING=utf-8
python app.py
pause
