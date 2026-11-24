@echo off
set WT=C:\Users\David\ac-site-clone\.factory-worktrees\preview-main
if not exist "%WT%" exit /b 1
netstat -ano | findstr /C:":8500" | findstr /I "LISTENING" >nul 2>&1
if %errorlevel%==0 exit /b 0
git -C "%WT%" fetch origin -q 2>nul
git -C "%WT%" checkout -q origin/main 2>nul
cd /d "%WT%"
set PYTHONPATH=.
start "" /min C:\Users\David\ac-clone-env\Scripts\python.exe -m acclone serve --host 127.0.0.1 --port 8500
