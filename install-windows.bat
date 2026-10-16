@echo off
rem Apartment Planner - double-click to install on Windows (same as ApartmentPlanner-Setup.exe).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1" -Source "%~dp0."
