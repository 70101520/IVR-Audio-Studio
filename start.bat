@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Installing IVR Studio dependencies...
  py -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 pause & exit /b 1
)
if not exist "data\users.db" (
  set "IVR_ADMIN_USER=admin"
  set /p "IVR_ADMIN_PASSWORD=Create the initial admin password: "
  if not defined IVR_ADMIN_PASSWORD exit /b 1
)
echo IVR Voice Studio: http://localhost:5000
start "" http://localhost:5000
.venv\Scripts\python.exe app.py
