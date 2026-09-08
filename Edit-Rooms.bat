@echo off
REM Launch the Room Mapping Editor. Double-click this file on the sync host.
REM Edits space_mapping.yaml in this folder (the file the nightly sync reads).
cd /d "%~dp0"
python editor.py
if errorlevel 1 pause
