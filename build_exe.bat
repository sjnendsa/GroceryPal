@echo off
title Build GroceryPal.exe
cd /d "%~dp0"
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --name GroceryPal --exclude-module playwright --add-data "templates;templates" app.py
copy /Y dist\GroceryPal.exe GroceryPal.exe
echo.
echo Done - GroceryPal.exe now matches the current source.
pause
