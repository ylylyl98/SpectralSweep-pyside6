@echo off
setlocal
cd /d %~dp0

git fetch
git checkout main
git pull

call run_ui.cmd
endlocal
