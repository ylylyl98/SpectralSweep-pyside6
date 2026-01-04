@echo off
setlocal
cd /d %~dp0

git pull
call run_ui.cmd

endlocal
