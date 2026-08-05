@echo off
python -m pip install build
if errorlevel 1 exit /b %errorlevel%
python -m build
