# streamlit_entry.py  (repo root)
import sys
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parent

# Ensure repo root is first on sys.path so "app" resolves to the folder package
root_str = str(ROOT)
if sys.path[0] != root_str:
    sys.path.insert(0, root_str)

# Run the real Streamlit script
runpy.run_path(str(ROOT / "app" / "ui_streamlit" / "main_ui.py"), run_name="__main__")
