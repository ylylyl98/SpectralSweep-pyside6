@echo off
setlocal
cd /d %~dp0

git fetch
git checkout stable
git pull
call run_ui.cmd

endlocal
