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

# --- Rotation helpers ---
def list_serial_ports():
    try:
        import serial.tools.list_ports as lp
        return [p.device for p in lp.comports()]
    except Exception:
        return []

def render_rotation_block(devices_dict, block_name: str, key_suffix: str):
    """
    Renders a rotation mount block and registers the device as devices_dict[block_name]
    key_suffix must be unique per block, e.g. "rot1", "rot2"
    """
    st.markdown(f"### Rotation Mount — {block_name}")
    rot_type = st.selectbox(
        "Type", ["<none>", "Thorlabs Elliptec", "Newport EPS300 (VISA RS-232/GPIB)"],
        key=f"type_{key_suffix}"
    )

    col_rot1, col_rot2 = st.columns(2)
    rot_com, rot_resource, rot_baud = "", "", None
    with col_rot1:
        if rot_type == "Thorlabs Elliptec":
            ports = ["<select>"] + list_serial_ports()
            rot_com = st.selectbox("COM Port", options=ports, key=f"com_{key_suffix}")
        elif rot_type == "Newport EPS300 (VISA RS-232/GPIB)":
            rot_resource = st.text_input("VISA Resource", "ASRL4::INSTR", key=f"visa_{key_suffix}")
    with col_rot2:
        if rot_type == "Newport EPS300 (VISA RS-232/GPIB)":
            rot_baud = st.number_input("Baud (RS-232)", min_value=1200, max_value=921600, value=19200, step=1200, key=f"baud_{key_suffix}")
        should_connect = st.checkbox("Connect", value=False, key=f"chk_{key_suffix}")

    handle_key = f"{key_suffix}_handle"

    def close_handle():
        if handle_key in st.session_state:
            try: st.session_state[handle_key].close()
            except Exception as e: print(f"{block_name} close error: {e}")
            del st.session_state[handle_key]

    if should_connect and rot_type != "<none>":
        if handle_key not in st.session_state:
            try:
                if rot_type == "Thorlabs Elliptec":
                    sel = st.session_state.get(f"com_{key_suffix}", "<select>")
                    if not sel or sel == "<select>":
                        raise RuntimeError("Select a valid COM port.")
                    from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation
                    st.session_state[handle_key] = ElliptecRotation(sel)
                else:
                    from app.devices.rotation_eps300_adapter import NewportEPS300
                    st.session_state[handle_key] = NewportEPS300(
                        st.session_state.get(f"visa_{key_suffix}", "ASRL4::INSTR"),
                        rm=get_rm(), baud=st.session_state.get(f"baud_{key_suffix}", None)
                    )
                st.sidebar.success(f"{block_name}: Connected")
            except Exception as e:
                close_handle()
                st.sidebar.error(f"{block_name}: {e}")

        if handle_key in st.session_state:
            devices_dict[block_name] = st.session_state[handle_key]
            st.markdown("---")
            tgt = st.number_input("Angle (deg)", min_value=-9999.0, max_value=9999.0, value=0.0, step=1.0, key=f"ang_{key_suffix}")
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("Go", key=f"go_{key_suffix}"):
                    try:
                        st.session_state[handle_key].move_to(float(tgt))
                        st.success(f"{block_name}: moved to {tgt:.2f}°")
                    except Exception as e:
                        st.error(f"{block_name}: Move failed: {e}")
            with c2:
                if st.button("Read", key=f"read_{key_suffix}"):
                    try:
                        pos = st.session_state[handle_key].get_position()
                        st.info(f"{block_name}: {pos:.3f}°")
                    except Exception as e:
                        st.error(f"{block_name}: Read failed: {e}")
            with c3:
                if st.button("Disconnect", key=f"disc_{key_suffix}"):
                    close_handle()
                    st.sidebar.info(f"{block_name}: Disconnected")
                    st.rerun()

    else:
        # toggle off → ensure closed
        if handle_key in st.session_state:
            close_handle()
            st.sidebar.info(f"{block_name}: Disconnected")

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
            from iv_automation import PyvisaInstrument, IVSetup, KeithControl

            def make_inst(addr, role=None):
                # Respect "<none>" as no termination for generic instruments
                term_arg = None if term == "" else term

                # Use KeithControl for gate/bias roles so X/Y channels are created
                if role in ("Vbg", "Vtg", "Vbias"):
                    # KeithControl connects itself and registers x/y as: role, measured_role, role_leakage
                    return KeithControl(address=addr, name=f"{role}_SMU", variable_name=role, rm=rm)

                # Fallback generic VISA instrument (no X channel)
                inst = PyvisaInstrument(address=addr, name=addr, termination=term_arg, rm=rm)
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
                if addr not in {vbg_src, vtg_src, vbias_src}:
                    inst_list.append(make_inst(addr))

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
    should_connect_stage = st.checkbox("Connect Stage", value=False, key="chk_stage")

    if should_connect_stage:
        if "stage_handle" not in st.session_state:
            try:
                from app.devices.stage_adapter import LinearStage
                st.session_state.stage_handle = LinearStage(stage_port)
                st.sidebar.success(f"Stage Ready: {stage_port}")
            except Exception as e:
                st.sidebar.error(f"Stage Error: {e}")
        if "stage_handle" in st.session_state:
            devices["stage"] = st.session_state.stage_handle
    else:
        if "stage_handle" in st.session_state:
            try:
                st.session_state.stage_handle.close()
            except Exception as e:
                print(f"Error closing stage: {e}")
            del st.session_state["stage_handle"]
            st.sidebar.info("Stage Closed & Released")

    # --- 2. POWER METER (PM100D) ---
    st.markdown("### Power Meter (PM100D)")
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

    if should_connect_pm:
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

        if "pm_handle" in st.session_state:
            pm = st.session_state.pm_handle
            devices["pm"] = pm
            
            st.markdown("---")
            c_wav_in, c_wav_set = st.columns([2, 1])
            with c_wav_in:
                target_wl = st.number_input("Wavelength (nm)", 
                                            min_value=400.0, max_value=1100.0, 
                                            value=730.0, step=10.0)
            with c_wav_set:
                st.write("")
                if st.button("Set λ"):
                    try:
                        pm.configure_wavelength(target_wl)
                        st.sidebar.success(f"Set to {target_wl:.0f} nm")
                    except Exception as e:
                        st.sidebar.error(f"Set Failed: {e}")
    else:
        if "pm_handle" in st.session_state:
            try: st.session_state.pm_handle.close()
            except: pass
            del st.session_state["pm_handle"]
            st.sidebar.info("PM Closed & Released")

    # --- 3. ROTATION MOUNT ---
    st.markdown("### Rotation Mount")
    render_rotation_block(devices, "rotation1", "rot1")
    render_rotation_block(devices, "rotation2", "rot2")
    rot_type = st.selectbox("Type", ["<none>", "Thorlabs Elliptec", "Newport EPS300 (VISA RS-232/GPIB)"])

    col_rot1, col_rot2 = st.columns(2)
    with col_rot1:
        if rot_type == "Thorlabs Elliptec":
            rot_com = st.text_input("COM Port (Elliptec)", "COM4")
        elif rot_type == "Newport EPS300 (VISA RS-232/GPIB)":
            rot_resource = st.text_input("VISA Resource", "ASRL6::INSTR")  # or "GPIB0::5::INSTR"
        else:
            rot_com = ""
            rot_resource = ""

    with col_rot2:
        if rot_type == "Newport EPS300 (VISA RS-232/GPIB)":
            rot_baud = st.number_input("Baud (RS-232 only)", min_value=1200, max_value=921600, value=19200, step=1200)
        else:
            rot_baud = None
        should_connect_rot = st.checkbox("Connect Rotation", value=False, key="chk_rot")

    if should_connect_rot and rot_type != "<none>":
        if "rot_handle" not in st.session_state:
            try:
                if rot_type == "Thorlabs Elliptec":
                    from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation
                    st.session_state.rot_handle = ElliptecRotation(rot_com)
                elif rot_type == "Newport EPS300 (VISA RS-232/GPIB)":
                    from app.devices.rotation_eps300_adapter import NewportEPS300
                    st.session_state.rot_handle = NewportEPS300(rot_resource, rm=get_rm(), baud=rot_baud)
                st.sidebar.success("Rotation Connected")
            except Exception as e:
                st.sidebar.error(f"Rotation Connect Error: {e}")

        if "rot_handle" in st.session_state:
            devices["rotation"] = st.session_state.rot_handle

            st.markdown("---")
            target_deg = st.number_input("Angle (deg)", min_value=-9999.0, max_value=9999.0, value=0.0, step=1.0)
            col_go, col_read = st.columns(2)
            with col_go:
                if st.button("Go", key="rot_go"):
                    try:
                        st.session_state.rot_handle.move_to(float(target_deg))
                        st.success(f"Moved to {target_deg:.2f}°")
                    except Exception as e:
                        st.error(f"Move Fail: {e}")
            with col_read:
                if st.button("Read", key="rot_read"):
                    try:
                        pos = st.session_state.rot_handle.get_position()
                        st.info(f"Position: {pos:.3f}°")
                    except Exception as e:
                        st.error(f"Read Fail: {e}")
    else:
        if "rot_handle" in st.session_state:
            try: st.session_state.rot_handle.close()
            except Exception as e: print(f"Error closing rotation: {e}")
            del st.session_state["rot_handle"]
            st.sidebar.info("Rotation Closed & Released")



    # --- MANUAL TEST (Uses whatever is currently connected) ---
    st.markdown("---")
    c_test_pm, c_test_stg, c_test_rot = st.columns(3)

    
    # --- Power meter quick read ---
    with c_test_pm:
        st.markdown("**PM Quick Read**")
        if st.button("Read Pwr"):
            pm = devices.get("pm")
            if pm:
                try:
                    st.info(f"{pm.get_power()*1e6:.2f} µW")
                except Exception as e:
                    st.error(f"Read Fail: {e}")
            else:
                st.warning("No PM")

    # --- Linear stage quick move ---
    with c_test_stg:
        st.markdown("**Stage Quick Move**")
        target_pos = st.number_input("Position (0-3600)",
                                    min_value=0.0, max_value=3600.0,
                                    value=0.0, step=10.0, key="stage_pos_test")
        if st.button("Move", key="stage_move_btn"):
            stg = devices.get("stage")
            if stg:
                try:
                    stg.move_to(target_pos)
                    st.success(f"Moved to {target_pos}")
                except Exception as e:
                    st.error(f"Move Fail: {e}")
            else:
                st.warning("No Stage")

    # --- Rotation mount quick test (supports rotation1 / rotation2) ---
    with c_test_rot:
        st.markdown("**Rotation Quick Test**")

        # discover connected rotation handles (work with either the single-key or dual-key setup)
        rot_options = []
        # devices dict (preferred if you already registered them earlier in this sidebar)
        for k in ("rotation", "rotation1", "rotation2"):
            if k in devices:
                rot_options.append(k)
        # fallback: session_state handles if not injected into devices yet
        if "rot1_handle" in st.session_state and "rotation1" not in rot_options:
            rot_options.append("rotation1")
            devices["rotation1"] = st.session_state["rot1_handle"]
        if "rot2_handle" in st.session_state and "rotation2" not in rot_options:
            rot_options.append("rotation2")
            devices["rotation2"] = st.session_state["rot2_handle"]

        if not rot_options:
            st.warning("No rotation mount connected.")
        else:
            which_rot = st.selectbox("Choose mount", rot_options, key="rot_choice_manual")
            rot_target = st.number_input("Angle (deg)", value=0.0, step=1.0, key="rot_target_manual")

            col_go, col_read, col_zero = st.columns(3)
            with col_go:
                if st.button("Go", key="rot_go_manual"):
                    try:
                        devices[which_rot].move_to(float(rot_target))
                        st.success(f"{which_rot}: moved to {rot_target:.2f}°")
                    except Exception as e:
                        st.error(f"{which_rot}: Move failed: {e}")
            with col_read:
                if st.button("Read", key="rot_read_manual"):
                    try:
                        pos = devices[which_rot].get_position()
                        st.info(f"{which_rot}: {pos:.3f}°")
                    except Exception as e:
                        st.error(f"{which_rot}: Read failed: {e}")
            with col_zero:
                # optional: convenience to go to 0°
                if st.button("Zero", key="rot_zero_manual"):
                    try:
                        devices[which_rot].move_to(0.0)
                        st.success(f"{which_rot}: moved to 0.00°")
                    except Exception as e:
                        st.error(f"{which_rot}: Zero failed: {e}")

# ---------------- Presets panel (main content) ----------------
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

# 3. Attach rotation 
if "rot_handle" in st.session_state:
    devices_main["rotation"] = st.session_state.rot_handle
    print("DEBUG: Rotation added to devices_main")

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

