@echo off
REM Run once. Makes the AI server start by itself every time you log in to Windows.
REM On the gateway PC it asks for the other PC's IP at every logon.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_startup.ps1"
pause
