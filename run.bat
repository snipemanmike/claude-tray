@echo off
REM Launch the dashboard silently. Uses pythonw from PATH (the no-console
REM variant of python) so no terminal window appears.
start "" /B pythonw "%~dp0usagedashboard.py"
