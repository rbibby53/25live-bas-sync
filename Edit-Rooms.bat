@echo off
REM Launch the Room Mapping Editor. Double-click this file on the BAS server.
REM Edits D:\BAS\space_mapping.yaml (the file the nightly sync reads).
cd /d "%~dp0"
python editor.py
if errorlevel 1 pause
