@echo off
set "APP=D:\instrument_control_v3_1\lab_runner_streamlit\app\ui_streamlit\main_ui.py"
set "ROOT=D:\instrument_control_v3_1\lab_runner_streamlit"
set "PORT=8502"

pushd "%ROOT%"
python -m streamlit run "%APP%" --server.address localhost --server.port %PORT% --server.headless false
rem Give Streamlit a moment to start, then open default browser
start "" http://localhost:%PORT%
popd
