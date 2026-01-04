# app/ui_streamlit/main_ui.py
# --- path shim ---
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

# Force-load step modules so @register decorators run
import app.steps._import_all  # noqa: F401

from app.ui_streamlit.views.presets import render as render_presets
from app.ui_streamlit.views.megasweep import render as render_megasweep
from app.devices.lf6_adapter import SpectrometerLF6
from app.devices.iv_adapter import IVDevice

import lf6_automation
import iv_automation

def scan_thorlabs_devices():
    """Finds devices using the Thorlabs TLPMX driver (DLL)."""
    try:
        # Import the file you uploaded (TLPMX.py)
        from TLPMX import TLPMX
        from ctypes import c_uint32, create_string_buffer, c_char_p, byref, c_int
        
        # Initialize driver
        tlPM = TLPMX()
        deviceCount = c_uint32()
        tlPM.findRsrc(byref(deviceCount))
        
        found_list = []
        if deviceCount.value > 0:
            # Buffer to hold the name
            resourceName = create_string_buffer(1024)
            for i in range(deviceCount.value):
                tlPM.getRsrcName(c_int(i), resourceName)
                # Convert C-bytes to Python String for the UI
                name_str = c_char_p(resourceName.raw).value.decode('ascii')
                found_list.append(name_str)
        
        # Close the temporary scanner instance
        try: tlPM.close()
        except: pass
        
        return found_list if found_list else ["<No devices found>"]

    except ImportError:
        return ["Error: TLPMX.py not found in project root"]
    except Exception as e:
        return [f"Error scanning: {e}"]

# ---------------- Cache helpers ----------------
@st.cache_resource(show_spinner=False)
def get_lf6():
    # Construct LF6Setup once per Streamlit server session
    return lf6_automation.LF6Setup()

@st.cache_resource(show_spinner=False)
def get_spec(_lf6):
    # Wrap LF6 as a spectrometer once
    return SpectrometerLF6(_lf6)

@st.cache_resource(show_spinner=False)
def get_rm():
    import pyvisa
    return pyvisa.ResourceManager()


# ---------------- Page setup ----------------
st.set_page_config(page_title="Lab Runner (Streamlit - Real Devices)", layout="wide")
st.sidebar.title("Device Configuration")

# Ensure session defaults BEFORE creating widgets (prevents the warning)
st.session_state.setdefault("lf6_ready", False)
st.session_state.setdefault("lf6_auto_load_on_connect", True)  # <-- default lives in session state


# ---------------- LF6 Spectrometer (deferred init) ----------------
with st.sidebar.expander("LF6 Spectrometer", expanded=True):
    # The checkbox uses only key=... (no value=), because default is in session_state
    lf_auto = st.checkbox("Auto-load experiment on connect", key="lf6_auto_load_on_connect")

    if not st.session_state.lf6_ready:
        st.info("LightField will not start until you click Connect.")
        if st.button("Connect LightField / Spectrometer", type="primary", key="connect_lf6"):
            lf6 = get_lf6()             # First call starts LightField process
            spec = get_spec(lf6)        # Wrap LF6
            st.session_state.lf6 = lf6
            st.session_state.spec = spec

            # Optionally auto-load last/current experiment
            try:
                if st.session_state.lf6_auto_load_on_connect:
                    # If you want to auto-load a specific name, replace with your choice
                    # lf6.load_experiment("YourExperimentName")
                    pass
            except Exception as e:
                st.warning(f"Auto-load experiment failed: {e}")

            st.session_state.lf6_ready = True
            st.rerun()
    else:
        lf6  = st.session_state.get("lf6")
        spec = st.session_state.get("spec")

        # Experiment controls
        exp_names = []
        try:
            exp_iter = lf6.experiment.GetSavedExperiments()
            exp_names = list(exp_iter) if exp_iter is not None else []
        except Exception:
            exp_names = []

        exp_name = st.selectbox("Saved Experiment", options=(exp_names or ["<Use default/current>"]))
        exposure_ms = st.number_input("Exposure (ms)", 1.0, 600000.0, 2000.0, 10.0)
        center_nm = st.number_input("Center λ (nm)", 200.0, 2000.0, 885.0, 0.1)

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("Load experiment"):
                try:
                    if exp_name != "<Use default/current>":
                        lf6.load_experiment(exp_name)
                        st.success(f"Loaded: {exp_name}")
                except Exception as e:
                    st.warning(f"Load failed: {e}")
        with col2:
            if st.button("Apply exposure/center"):
                try:
                    if hasattr(lf6, "change_expose_time"):
                        lf6.change_expose_time(exposure_ms)
                    if hasattr(lf6, "change_spectra_center"):
                        lf6.change_spectra_center(center_nm)
                    st.caption("Exposure/center applied.")
                except Exception as e:
                    st.warning(f"Apply failed: {e}")
        with col3:
            if st.button("Disconnect LF6"):
                # Optional: add your teardown if LF exposes one
                try:
                    # lf6.close()  # if you have such a method
                    pass
                except Exception:
                    pass
                # Clear cached resources to allow a fresh connect
                try:
                    get_lf6.clear()
                    get_spec.clear()
                except Exception:
                    pass
                st.session_state.lf6_ready = False
                st.session_state.lf6 = None
                st.session_state.spec = None
                st.experimental_rerun()

        st.sidebar.success("LF6 connected.")


# ---------------- VISA (IV) discovery ----------------
with st.sidebar.expander("IV Instruments (VISA)", expanded=False):
    try:
        rm = get_rm()
        try:
            resources = list(rm.list_resources())
        except Exception as e:
            st.warning(f"VISA list_resources failed: {e}")
            resources = []
    except Exception as e:
        st.warning(f"PyVISA not available: {e}")
        resources = []
        rm = None

    st.caption("Select VISA resources to include in IV setup.")
    selected = st.multiselect("Resources", options=resources, default=[])

    term_choice = st.selectbox(
        "Termination", options=["\\n", "\\r", "\\r\\n", "<none>"], index=0,
        help="Write/read line termination used by your instrument"
    )
    term = "" if term_choice == "<none>" else term_choice
    timeout_ms = st.number_input("Timeout (ms)", min_value=100, max_value=30000, value=3000, step=100)

    # Safer VISA tester (non-blocking style)
    open_only = st.checkbox("Open only (no *IDN?)", value=False,
                            help="If a device hangs on *IDN?, try open-only first.")
    results_box = st.empty()

    if st.button("Test selected VISA", key="visa_test_safe"):
        if not selected or rm is None:
            st.info("Select at least one VISA resource to test.")
        else:
            rows = []
            for addr in selected:
                status, info = "ERROR", ""
                try:
                    res = rm.open_resource(addr)
                    res.timeout = min(timeout_ms, 1500)
                    res.read_termination  = term if term else None
                    res.write_termination = term if term else None

                    if open_only:
                        status, info = "OK", "Opened session"
                    else:
                        try:
                            idn = res.query("*IDN?").strip()
                        except Exception:
                            res.write("*IDN?")
                            idn = res.read().strip()
                        status, info = "OK", idn
                except Exception as e:
                    status, info = "ERROR", str(e)
                finally:
                    try:
                        res.close()
                    except Exception:
                        pass

                rows.append((addr, status, info))
                results_box.write("\n".join(
                    f"{'✅' if s=='OK' else '❌'} {a} → {i}" for a,s,i in rows
                ))

            st.caption("VISA test results")
            for addr, status, info in rows:
                (st.success if status == "OK" else st.error)(f"{addr} → {info}")

    # Role mapping (no linking logic)
    st.markdown("**Channel mapping (roles)**")
    vbg_src   = st.selectbox("Vbg source",   options=["<none>"] + selected, index=0, key="vbg_src")
    vtg_src   = st.selectbox("Vtg source",   options=["<none>"] + selected, index=0, key="vtg_src")
    vbias_src = st.selectbox("Vbias source", options=["<none>"] + selected, index=0, key="vbias_src")

    # Build IV setup if any devices selected
    devices = {}
    spec = st.session_state.get("spec")
    if st.session_state.get("lf6_ready") and spec is not None:
        devices["spectrometer"] = spec

    try:
        if selected and rm is not None:
            from iv_automation import PyvisaInstrument, IVSetup

            def make_inst(addr, name_override=None):
                inst = PyvisaInstrument(address=addr, name=name_override or addr, termination=term, rm=rm)
                try:
                    inst.timeout = timeout_ms
                except Exception:
                    pass
                return inst

            inst_list = []
            if vbg_src   != "<none>": inst_list.append(make_inst(vbg_src,   "Vbg"))
            if vtg_src   != "<none>": inst_list.append(make_inst(vtg_src,   "Vtg"))
            if vbias_src != "<none>": inst_list.append(make_inst(vbias_src, "Vbias"))

            # Any other selected devices (optional nicknames omitted for brevity)
            for addr in selected:
                if addr in {vbg_src, vtg_src, vbias_src}:
                    continue
                inst_list.append(make_inst(addr, name_override=addr))

            iv_setup = IVSetup(inst_list)
            from app.devices.iv_adapter import IVDevice
            role_map = {
                "Vbg":   vbg_src   if vbg_src   != "<none>" else None,
                "Vtg":   vtg_src   if vtg_src   != "<none>" else None,
                "Vbias": vbias_src if vbias_src != "<none>" else None,
            }
            iv = IVDevice(iv_setup, role_map=role_map)
            devices["iv"] = iv

            mapped = []
            if role_map["Vbg"]:   mapped.append(f"Vbg→{role_map['Vbg']}")
            if role_map["Vtg"]:   mapped.append(f"Vtg→{role_map['Vtg']}")
            if role_map["Vbias"]: mapped.append(f"Vbias→{role_map['Vbias']}")
            st.success("IV ready: " + (", ".join(mapped) if mapped else "no roles set"))
        else:
            st.info("No VISA devices selected. Gates/bias will be ignored.")
    except Exception as e:
        st.warning(f"IV init failed: {e}")

# ---------------- Auxiliary Hardware (With Close/Reset Logic) ----------------
with st.sidebar.expander("Auxiliary Hardware", expanded=False):
    
    # --- 1. THORLABS STAGE ---
    st.markdown("### Thorlabs Stage")
    stage_port = st.text_input("Stage Port", "COM5")
    
    # We use a key to track the checkbox state
    should_connect_stage = st.checkbox("Connect Stage", value=False, key="chk_stage")

    # LOGIC: Connect vs Disconnect
    if should_connect_stage:
        # A. CONNECT (If not already connected)
        if "stage_handle" not in st.session_state:
            try:
                from app.devices.stage_adapter import LinearStage
                st.session_state.stage_handle = LinearStage(stage_port)
                st.sidebar.success(f"Stage Ready: {stage_port}")
            except Exception as e:
                st.sidebar.error(f"Stage Error: {e}")
        
        # Register device for use in experiments
        if "stage_handle" in st.session_state:
            devices["stage"] = st.session_state.stage_handle

    else:
        # B. DISCONNECT (If it was connected, close it now)
        if "stage_handle" in st.session_state:
            try:
                st.session_state.stage_handle.close()
            except Exception as e:
                print(f"Error closing stage: {e}")
            
            # Remove from memory so next check forces a fresh connection
            del st.session_state["stage_handle"]
            st.sidebar.info("Stage Closed & Released")


    # --- 2. POWER METER (PM100D) ---
    st.markdown("### Power Meter (PM100D)")
    
    # Device Selector
    if "tl_devices" not in st.session_state:
        st.session_state.tl_devices = ["<Click Scan>"]

    col_scan, col_sel = st.columns([1, 2])
    with col_scan:
        if st.button("Scan"):
            devs = scan_thorlabs_devices()
            st.session_state.tl_devices = devs if devs else ["<No devices found>"]
            st.rerun() 
            
    with col_sel:
        pm_resource_str = st.selectbox("Device", st.session_state.tl_devices)

    should_connect_pm = st.checkbox("Connect Power Meter", value=False, key="chk_pm")

    # LOGIC: Connect vs Disconnect
    if should_connect_pm:
        # A. CONNECT
        if "pm_handle" not in st.session_state:
            if pm_resource_str and "<" not in pm_resource_str:
                try:
                    from app.devices.pm100d_adapter import ThorlabsPM100D_Wrapper
                    st.session_state.pm_handle = ThorlabsPM100D_Wrapper(pm_resource_str)
                    st.sidebar.success("PM Connected")
                except Exception as e:
                    st.sidebar.error(f"PM Connect Error: {e}")
            else:
                st.sidebar.warning("Invalid Device Selected")

        # Register device
        if "pm_handle" in st.session_state:
            pm = st.session_state.pm_handle
            devices["pm"] = pm
            
            # --- NEW: WAVELENGTH SETTING ---
            st.markdown("---")
            c_wav_in, c_wav_set = st.columns([2, 1])
            with c_wav_in:
                target_wl = st.number_input("Wavelength (nm)", 
                                            min_value=400.0, max_value=1100.0, 
                                            value=730.0, step=10.0)
            with c_wav_set:
                st.write("") # Spacer
                if st.button("Set λ"):
                    try:
                        pm.configure_wavelength(target_wl)
                        st.sidebar.success(f"Set to {target_wl:.0f} nm")
                    except Exception as e:
                        st.sidebar.error(f"Set Failed: {e}")

    else:
        # B. DISCONNECT
        if "pm_handle" in st.session_state:
            try: st.session_state.pm_handle.close()
            except: pass
            del st.session_state["pm_handle"]
            st.sidebar.info("PM Closed & Released")

    # --- MANUAL TEST (Uses whatever is currently connected) ---
    st.markdown("---")
    c_test_pm, c_test_stg = st.columns(2)
    
    with c_test_pm:
        if st.button("Read Pwr"):
            pm = devices.get("pm")
            if pm:
                try: st.info(f"{pm.get_power()*1e6:.2f} µW")
                except: st.error("Read Fail")
            else: st.warning("No PM")

    with c_test_stg:
        target_pos = st.number_input("Position (0-3600)", 
                                     min_value=0.0, max_value=3600.0, 
                                     value=0.0, step=10.0)
        if st.button("Move"):
            stg = devices.get("stage")
            if stg:
                try: 
                    stg.move_to(target_pos)
                    st.success(f"Moved to {target_pos}")
                except Exception as e: 
                    st.error(f"Move Fail: {e}")
            else: 
                st.warning("No Stage")

# ---------------- Presets panel (main content) ----------------
# Only require spectrometer when connected; allow IV-only usage too.
devices_main = {}
spec = st.session_state.get("spec")
if spec is not None and st.session_state.get("lf6_ready"):
    devices_main["spectrometer"] = spec
if "iv" in locals() and isinstance(iv, IVDevice):
    devices_main["iv"] = iv

# Wavelength headers required only if spectrometer present
if "spectrometer" in devices_main:
    try:
        wavelength_headers = devices_main["spectrometer"].calibration_wavelengths()
    except Exception as e:
        st.error(f"Failed to read wavelength calibration: {e}")
        wavelength_headers = []
else:
    wavelength_headers = []

# Scalars to include in CSV (no leakage columns)
extra_scalar_fields_order = ["Vbias"]

# ---------------------------------------------------------
# BRIDGE: Pass Sidebar Connections to the Experiment Engine
# ---------------------------------------------------------

# 1. Attach Stage
if "stage_handle" in st.session_state:
    # CHANGE 'devices' TO 'devices_main'
    devices_main["stage"] = st.session_state.stage_handle 
    print("DEBUG: Stage added to devices_main")

# 2. Attach Power Meter
if "pm_handle" in st.session_state:
    # CHANGE 'devices' TO 'devices_main'
    devices_main["pm"] = st.session_state.pm_handle
    print("DEBUG: PM added to devices_main")

# ---------------------------------------------------------

# # Render measurement presets (Now it has the stage!)
# render_presets(devices_main, wavelength_headers, extra_scalar_fields_order)

# ---------------------------------------------------------
# NAVIGATION & ROUTING
# ---------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.title("Experiment Selection")

# 1. Add the Mode Switcher to the Sidebar
app_mode = st.sidebar.radio(
    "Select Mode",
    ["Dual Gate Sweep", "MegaSweep"]
)

# 2. Route to the correct Page
if app_mode == "Dual Gate Sweep":
    # This calls the existing code in presets.py
    render_presets(devices_main, wavelength_headers, extra_scalar_fields_order)

elif app_mode == "MegaSweep":
    # Define your new interface here (or import a function from another file)
    render_megasweep(devices_main)

