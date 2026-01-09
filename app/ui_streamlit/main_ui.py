# app/ui_streamlit/main_ui.py
# ──────────────────────────────────────────────────────────────────────────────
# Streamlit main UI for lab runner
# - All device addresses via dropdowns (COM / VISA) with Refresh
# - Rotation mounts: 2 independent blocks (Elliptec or Newport ESP300)
#   • Connection blocks: type, address, axis scan/selection, connect/disconnect
#   • NO movement/read controls in connection blocks
# - Linear stage: Thorlabs Elliptec or Newport ESP300 (axis auto-probed)
# - ESP300: auto-detect available axes on connect, motor ON for all detected axes
# - Quick Test panel (PM / Stage / Rotation) is separate from connection panels
# - IV instruments mapping and LF6 spectrometer management included
# ──────────────────────────────────────────────────────────────────────────────

# --- Path shim so "app" package resolves when launched via streamlit_entry.py
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


# ──────────────────────────────────────────────────────────────────────────────
# CACHED HELPERS (Streamlit caches these singletons per server session)
# ──────────────────────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def get_lf6():
    """Create LF6Setup (starts LightField once per server session)."""
    return lf6_automation.LF6Setup()

@st.cache_resource(show_spinner=False)
def get_spec(_lf6):
    """Wrap LF6 into a spectrometer adapter once."""
    return SpectrometerLF6(_lf6)

@st.cache_resource(show_spinner=False)
def get_rm():
    """Get a global VISA ResourceManager."""
    import pyvisa
    return pyvisa.ResourceManager()


# ──────────────────────────────────────────────────────────────────────────────
# LOW-LEVEL DISCOVERY HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def list_serial_ports():
    """
    Returns a list of available COM ports, e.g. ["COM3", "COM4"].
    Used for Thorlabs Elliptec devices.
    """
    try:
        import serial.tools.list_ports as lp
        return [p.device for p in lp.comports()]
    except Exception:
        return []

def list_visa_resources(prefixes=("ASRL", "GPIB", "USB")):
    """
    Returns a filtered list of VISA resources (e.g., ["ASRL6::INSTR","GPIB0::5::INSTR"]).
    Default filters: serial (ASRL), GPIB, USB.
    """
    try:
        import pyvisa
        rm = pyvisa.ResourceManager()
        all_res = list(rm.list_resources())
        return [r for r in all_res if any(r.startswith(p) for p in prefixes)]
    except Exception:
        return []

def scan_thorlabs_devices():
    """
    Finds devices using Thorlabs TLPMX driver (DLL). This is for the PM100D list.
    If TLPMX.py isn't present or fails, return a friendly message instead of crashing.
    """
    try:
        from TLPMX import TLPMX
        from ctypes import c_uint32, create_string_buffer, c_char_p, byref, c_int
        tlPM = TLPMX()
        deviceCount = c_uint32()
        tlPM.findRsrc(byref(deviceCount))

        found_list = []
        if deviceCount.value > 0:
            resourceName = create_string_buffer(1024)
            for i in range(deviceCount.value):
                tlPM.getRsrcName(c_int(i), resourceName)
                name_str = c_char_p(resourceName.raw).value.decode('ascii')
                found_list.append(name_str)

        try:
            tlPM.close()
        except Exception:
            pass

        return found_list if found_list else ["<No devices found>"]

    except ImportError:
        return ["Error: TLPMX.py not found in project root"]
    except Exception as e:
        return [f"Error scanning: {e}"]


# ──────────────────────────────────────────────────────────────────────────────
# ROTATION BLOCK (connection only — no movement/read controls here)
# ──────────────────────────────────────────────────────────────────────────────

def render_rotation_block(devices_dict, block_name: str, key_suffix: str):
    """
    Render a rotation mount connection block and register its handle in devices_dict.

    - key_suffix must be unique ("rot1", "rot2") to isolate Streamlit widget keys.
    - Type selection: <none> | Thorlabs Elliptec | Newport ESP300 (VISA).
    - Address selection is via dropdowns with Refresh.
    - For ESP300: optional axis picker is shown; on connect we will:
        • probe available axes
        • set active_axis
        • motor ON for all detected axes (best-effort)
    - NO angle/Go/Read controls here — those live in the Quick Test panel.
    """
    st.markdown(f"### Rotation Mount — {block_name}")

    # Pick rotation type
    rot_type = st.selectbox(
        "Type", ["<none>", "Thorlabs Elliptec", "Newport ESP300 (VISA RS-232/GPIB)"],
        key=f"type_{key_suffix}"
    )

    # Address selection (dropdown + Refresh)
    col_addr1, col_addr2 = st.columns([2, 1])
    should_connect = False
    if rot_type == "Thorlabs Elliptec":
        # COM dropdown
        if st.button("Refresh COMs", key=f"refresh_com_{key_suffix}"):
            st.session_state[f"com_list_{key_suffix}"] = list_serial_ports()
        com_list = st.session_state.get(f"com_list_{key_suffix}", None)
        if com_list is None:
            com_list = list_serial_ports()
            st.session_state[f"com_list_{key_suffix}"] = com_list
        com_options = ["<select>"] + com_list
        with col_addr1:
            st.selectbox("COM Port", options=com_options, index=0, key=f"com_{key_suffix}",
                         help="Pick the serial port for Elliptec")
        with col_addr2:
            should_connect = st.checkbox("Connect", value=False, key=f"chk_{key_suffix}")

    elif rot_type == "Newport ESP300 (VISA RS-232/GPIB)":
        # VISA dropdown
        if st.button("Refresh VISA", key=f"refresh_visa_{key_suffix}"):
            st.session_state[f"visa_list_{key_suffix}"] = list_visa_resources(("ASRL", "GPIB", "USB"))
        visa_list = st.session_state.get(f"visa_list_{key_suffix}", None)
        if visa_list is None:
            visa_list = list_visa_resources(("ASRL", "GPIB", "USB"))
            st.session_state[f"visa_list_{key_suffix}"] = visa_list
        visa_options = ["<select>"] + visa_list
        with col_addr1:
            st.selectbox("VISA Resource", options=visa_options, index=0, key=f"visa_{key_suffix}",
                         help="Pick the ESP300 VISA address (ASRL.., GPIB.., or USB..)")
        with col_addr2:
            should_connect = st.checkbox("Connect", value=False, key=f"chk_{key_suffix}")
    else:
        # <none> → no controls
        pass

    # Optional axis picker (visible only for ESP300)
    axis_list_key = f"{key_suffix}_axes"
    if rot_type == "Newport ESP300 (VISA RS-232/GPIB)":
        if axis_list_key not in st.session_state:
            st.session_state[axis_list_key] = [1]
        c_scan_ax, c_ax_sel = st.columns([1, 1])

        with c_scan_ax:
            if st.button("Scan Axes", key=f"btn_scan_axes_{key_suffix}"):
                try:
                    from app.devices.rotation_eps300_adapter import NewportEPS300
                    visa_sel = st.session_state.get(f"visa_{key_suffix}", "<select>")
                    if visa_sel == "<select>":
                        raise RuntimeError("Select a VISA resource first.")
                    tmp = NewportEPS300(visa_sel, rm=get_rm())
                    axes = tmp.get_axes(conservative=True) or [1]
                    st.session_state[axis_list_key] = axes
                    try:
                        tmp.close()
                    except Exception:
                        pass
                    st.success(f"{block_name}: Found axes {axes}")
                except Exception as e:
                    st.session_state[axis_list_key] = [1]
                    st.warning(f"{block_name} axis scan failed: {e}")

        with c_ax_sel:
            st.selectbox("Axis", st.session_state[axis_list_key], key=f"axis_{key_suffix}",
                         help="Axis to use for this rotation mount")

    # Utility for closing a handle cleanly
    handle_key = f"{key_suffix}_handle"

    def close_handle():
        if handle_key in st.session_state:
            try:
                st.session_state[handle_key].close()
            except Exception as e:
                print(f"{block_name} close error: {e}")
            del st.session_state[handle_key]

    # Connect / Disconnect (NO test controls here)
    if should_connect and rot_type != "<none>":
        if handle_key not in st.session_state:
            try:
                if rot_type == "Thorlabs Elliptec":
                    sel = st.session_state.get(f"com_{key_suffix}", "<select>")
                    if not sel or sel == "<select>":
                        raise RuntimeError("Please select a COM port.")
                    from app.devices.rotation_thorlabs_elliptec_adapter import ElliptecRotation
                    st.session_state[handle_key] = ElliptecRotation(sel)

                else:
                    # Newport ESP300
                    visa_sel = st.session_state.get(f"visa_{key_suffix}", "<select>")
                    if not visa_sel or visa_sel == "<select>":
                        raise RuntimeError("Please select a VISA resource.")
                    from app.devices.rotation_eps300_adapter import NewportEPS300
                    import inspect
                    eps = NewportEPS300(visa_sel, rm=get_rm())

                    # Probe available axes immediately on connect
                    try:
                        axes = eps.get_axes(conservative=True) or [1]
                        st.session_state[axis_list_key] = axes
                    except Exception:
                        axes = [1]

                    # Set active axis from UI (default first axis)
                    axis_ui = int(st.session_state.get(f"axis_{key_suffix}", axes[0]))
                    try:
                        setattr(eps, "active_axis", axis_ui)
                    except Exception:
                        pass

                    # Motor ON for all detected axes (best-effort)
                    try:
                        if hasattr(eps, "motor_on"):
                            sig = inspect.signature(eps.motor_on)
                            if "axis" in sig.parameters:
                                for axx in axes:
                                    try:
                                        eps.motor_on(axis=axx)
                                    except Exception:
                                        pass
                            else:
                                eps.motor_on()
                    except Exception as e:
                        st.warning(f"{block_name}: Motor ON failed on some axes: {e}")

                    st.session_state[handle_key] = eps

                st.sidebar.success(f"{block_name}: Connected")

            except Exception as e:
                close_handle()
                st.sidebar.error(f"{block_name}: {e}")

        # Register to devices if connected
        if handle_key in st.session_state:
            devices_dict[block_name] = st.session_state[handle_key]

    else:
        # If toggled off, ensure closed
        if handle_key in st.session_state:
            close_handle()
            st.sidebar.info(f"{block_name}: Disconnected")


# ──────────────────────────────────────────────────────────────────────────────
# PAGE SETUP
# ──────────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Lab Runner (Streamlit - Real Devices)", layout="wide")
st.sidebar.title("Device Configuration")

# Session defaults to avoid "widget changed after creation" warnings
st.session_state.setdefault("lf6_ready", False)
st.session_state.setdefault("lf6_auto_load_on_connect", True)

# Create a single devices dict early so all panels can add to it
devices = {}


# ──────────────────────────────────────────────────────────────────────────────
# LF6 SPECTROMETER (deferred init)
# ──────────────────────────────────────────────────────────────────────────────

with st.sidebar.expander("LF6 Spectrometer", expanded=True):
    st.checkbox("Auto-load experiment on connect", key="lf6_auto_load_on_connect")

    if not st.session_state.lf6_ready:
        st.info("LightField will not start until you click Connect.")
        if st.button("Connect LightField / Spectrometer", type="primary", key="connect_lf6"):
            lf6 = get_lf6()
            spec = get_spec(lf6)
            st.session_state.lf6 = lf6
            st.session_state.spec = spec

            try:
                if st.session_state.lf6_auto_load_on_connect:
                    pass
            except Exception as e:
                st.warning(f"Auto-load experiment failed: {e}")

            st.session_state.lf6_ready = True
            st.rerun()
    else:
        lf6  = st.session_state.get("lf6")
        spec = st.session_state.get("spec")

        # Experiment controls
        try:
            exp_iter = lf6.experiment.GetSavedExperiments()
            exp_names = list(exp_iter) if exp_iter is not None else []
        except Exception:
            exp_names = []

        exp_name   = st.selectbox("Saved Experiment", options=(exp_names or ["<Use default/current>"]))
        exposure_ms = st.number_input("Exposure (ms)", 1.0, 600000.0, 2000.0, 10.0)
        center_nm   = st.number_input("Center λ (nm)", 200.0, 2000.0, 885.0, 0.1)

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
        if spec is not None:
            devices["spectrometer"] = spec


# ──────────────────────────────────────────────────────────────────────────────
# VISA (IV) DISCOVERY & ROLE MAPPING
# ──────────────────────────────────────────────────────────────────────────────

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

    # Role mapping
    st.markdown("**Channel mapping (roles)**")
    vbg_src   = st.selectbox("Vbg source",   options=["<none>"] + selected, index=0, key="vbg_src")
    vtg_src   = st.selectbox("Vtg source",   options=["<none>"] + selected, index=0, key="vtg_src")
    vbias_src = st.selectbox("Vbias source", options=["<none>"] + selected, index=0, key="vbias_src")

    # Build IV setup if any devices selected
    try:
        if selected and rm is not None:
            from iv_automation import PyvisaInstrument, IVSetup, KeithControl

            def make_inst(addr, role=None):
                term_arg = None if term == "" else term
                if role in ("Vbg", "Vtg", "Vbias"):
                    return KeithControl(address=addr, name=f"{role}_SMU", variable_name=role, rm=rm)
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
            for addr in selected:
                if addr not in {vbg_src, vtg_src, vbias_src}:
                    inst_list.append(make_inst(addr))

            iv_setup = IVSetup(inst_list)
            role_map = {"Vbg": vbg_src if vbg_src != "<none>" else None,
                        "Vtg": vtg_src if vtg_src != "<none>" else None,
                        "Vbias": vbias_src if vbias_src != "<none>" else None}
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


# ──────────────────────────────────────────────────────────────────────────────
# AUXILIARY HARDWARE (connect panels)
# ──────────────────────────────────────────────────────────────────────────────

with st.sidebar.expander("Auxiliary Hardware", expanded=False):

    # 1) LINEAR STAGE — Thorlabs Elliptec OR Newport ESP300
    st.markdown("### Linear Stage")
    stage_type = st.selectbox(
        "Stage Type",
        ["<none>", "Thorlabs Linear (Elliptec)", "Newport ESP300 (VISA RS-232/GPIB)"],
        key="stage_type",
    )

    # Address dropdowns
    col_stage_cfg1, col_stage_cfg2 = st.columns([2, 1])
    with col_stage_cfg1:
        if stage_type == "Thorlabs Linear (Elliptec)":
            if st.button("Refresh COMs", key="refresh_stage_com"):
                st.session_state["stage_com_list"] = list_serial_ports()
            coms = st.session_state.get("stage_com_list", None)
            if coms is None:
                coms = list_serial_ports()
                st.session_state["stage_com_list"] = coms
            st.selectbox("COM Port", options=(["<select>"] + coms), index=0, key="stage_com",
                         help="Select the serial port for the Thorlabs linear stage")

        elif stage_type == "Newport ESP300 (VISA RS-232/GPIB)":
            if st.button("Refresh VISA", key="refresh_stage_visa"):
                st.session_state["stage_visa_list"] = list_visa_resources(("ASRL", "GPIB", "USB"))
            visas = st.session_state.get("stage_visa_list", None)
            if visas is None:
                visas = list_visa_resources(("ASRL", "GPIB", "USB"))
                st.session_state["stage_visa_list"] = visas
            st.selectbox("VISA Resource", options=(["<select>"] + visas), index=0, key="stage_visa",
                         help="Pick the ESP300 controller's VISA address")
        else:
            # <none>
            pass

    with col_stage_cfg2:
        should_connect_stage = st.checkbox("Connect Stage", value=False, key="chk_stage")

    # ESP300 axis discovery (on-demand for the Stage)
    if stage_type == "Newport ESP300 (VISA RS-232/GPIB)":
        if "stage_axes" not in st.session_state:
            st.session_state.stage_axes = [1]
        c_scan_ax, c_ax_sel = st.columns([1, 1])
        with c_scan_ax:
            if st.button("Scan Axes", key="btn_scan_stage_axes"):
                try:
                    from app.devices.rotation_eps300_adapter import NewportEPS300
                    visa_sel = st.session_state.get("stage_visa", "<select>")
                    if visa_sel == "<select>":
                        raise RuntimeError("Select a VISA resource first.")
                    tmp = NewportEPS300(visa_sel, rm=get_rm())
                    axes = tmp.get_axes(conservative=True) or [1]
                    st.session_state.stage_axes = axes
                    try:
                        tmp.close()
                    except Exception:
                        pass
                    st.success(f"Found axes: {axes}")
                except Exception as e:
                    st.session_state.stage_axes = [1]
                    st.warning(f"Axis scan failed: {e}")
        with c_ax_sel:
            stage_axis = st.selectbox("Axis", st.session_state.stage_axes, key="stage_axis")

    # Utilities to close the stage cleanly
    def _close_stage_handle():
        if "stage_handle" in st.session_state:
            try:
                st.session_state.stage_handle.close()
            except Exception as _e:
                print(f"Stage close error: {_e}")
            del st.session_state["stage_handle"]
            st.session_state.pop("stage_meta", None)

    # Connect the stage
    if should_connect_stage and stage_type != "<none>":
        if "stage_handle" not in st.session_state:
            try:
                if stage_type == "Thorlabs Linear (Elliptec)":
                    sel = st.session_state.get("stage_com", "<select>")
                    if not sel or sel == "<select>":
                        raise RuntimeError("Please select a COM port.")
                    from app.devices.stage_adapter import LinearStage
                    st.session_state.stage_handle = LinearStage(sel)
                    st.session_state.stage_meta = {"type": "thorlabs"}

                else:
                    from app.devices.rotation_eps300_adapter import NewportEPS300
                    import inspect

                    visa_sel = st.session_state.get("stage_visa", "<select>")
                    if not visa_sel or visa_sel == "<select>":
                        raise RuntimeError("Please select a VISA resource.")
                    eps = NewportEPS300(visa_sel, rm=get_rm())

                    # Probe available axes and store
                    try:
                        axes = eps.get_axes(conservative=True) or [1]
                        st.session_state.stage_axes = axes
                    except Exception:
                        axes = [1]

                    # Choose UI axis or default first
                    ax = int(st.session_state.get("stage_axis", axes[0]))

                    # Try to store a default/active axis on the adapter
                    try:
                        setattr(eps, "active_axis", ax)
                    except Exception:
                        pass

                    # ALWAYS turn motor ON on connect for all detected axes
                    try:
                        if hasattr(eps, "motor_on"):
                            sig = inspect.signature(eps.motor_on)
                            if "axis" in sig.parameters:
                                for axx in axes:
                                    try:
                                        eps.motor_on(axis=axx)
                                    except Exception:
                                        pass
                            else:
                                eps.motor_on()
                    except Exception as e:
                        st.warning(f"Motor ON failed: {e}")

                    st.session_state.stage_handle = eps
                    st.session_state.stage_meta = {"type": "newport", "axis": ax}

                st.sidebar.success("Stage Connected")
            except Exception as e:
                _close_stage_handle()
                st.sidebar.error(f"Stage Connect Error: {e}")

        if "stage_handle" in st.session_state:
            devices["stage"] = st.session_state.stage_handle
    else:
        if "stage_handle" in st.session_state:
            _close_stage_handle()
            st.sidebar.info("Stage Closed & Released")

    # 2) POWER METER — Thorlabs PM100D via TLPMX
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
                target_wl = st.number_input(
                    "Wavelength (nm)",
                    min_value=400.0, max_value=1100.0,
                    value=730.0, step=10.0
                )
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
            try:
                st.session_state.pm_handle.close()
            except Exception:
                pass
            del st.session_state["pm_handle"]
            st.sidebar.info("PM Closed & Released")

    # 3) ROTATION MOUNTS — two independent blocks (connection only)
    st.markdown("### Rotation Mounts")
    render_rotation_block(devices, "rotation1", "rot1")
    render_rotation_block(devices, "rotation2", "rot2")


# ──────────────────────────────────────────────────────────────────────────────
# QUICK TEST PANEL (outside of the connection panels)
# ──────────────────────────────────────────────────────────────────────────────

    st.markdown("---")
    col_pm, col_stage = st.columns([1, 1])

    # A) PM quick read
    with col_pm:
        st.markdown("**PM Quick Read**")
        if st.button("Read Pwr", use_container_width=True):
            pm = devices.get("pm")
            if pm:
                try:
                    st.info(f"{pm.get_power()*1e6:.2f} µW")
                except Exception as e:
                    st.error(f"Read Fail: {e}")
            else:
                st.warning("No PM")

    # B) Stage quick move with auto limits
    with col_stage:
        st.markdown("**Stage Quick Move**")

        def _detect_stage_limits(stage):
            """
            Probe for travel limits from whatever adapter is connected.
            Returns (min_pos, max_pos). Falls back to (0, 3600).
            """
            # 1. Explicit overrides based on the adapter class name
            name = type(stage).__name__
            
            if "NewportEPS300" in name:
                return 0.0, 50.0  # Force 0-50 for Newport
            
            if "LinearStage" in name: 
                return 0.0, 3600.0 # Force 0-3600 for Thorlabs

            # 2. Probe standard methods (existing logic)
            try:
                if hasattr(stage, "get_soft_limits"):
                    lo, hi = stage.get_soft_limits()
                    return float(lo), float(hi)
            except Exception:
                pass
            
            try:
                if hasattr(stage, "get_soft_limits"):
                    lo, hi = stage.get_soft_limits()
                    return float(lo), float(hi)
            except Exception:
                pass
            try:
                if hasattr(stage, "get_travel_range"):
                    lo, hi = stage.get_travel_range()
                    return float(lo), float(hi)
            except Exception:
                pass
            try:
                if hasattr(stage, "get_limits"):
                    lo, hi = stage.get_limits()
                    return float(lo), float(hi)
            except Exception:
                pass
            try:
                if hasattr(stage, "get_max_position"):
                    hi = stage.get_max_position()
                    return 0.0, float(hi)
            except Exception:
                pass
            try:
                if hasattr(stage, "travel_mm"):
                    return 0.0, float(getattr(stage, "travel_mm"))
                if hasattr(stage, "travel_um"):
                    return 0.0, float(getattr(stage, "travel_um")) / 1000.0
            except Exception:
                pass
            try:
                if hasattr(stage, "get_axis_limits"):
                    lo, hi = stage.get_axis_limits()
                    return float(lo), float(hi)
            except Exception:
                pass
            return 0.0, 3600.0

        stg = devices.get("stage")
        if stg:
            lo_lim, hi_lim = _detect_stage_limits(stg)
            stage_target = st.number_input(
                f"Position ({lo_lim:.3f} – {hi_lim:.3f})",
                min_value=float(lo_lim),
                max_value=float(hi_lim),
                value=float(lo_lim),
                step=1.0,
                key="stage_pos_test",
            )
            if st.button("Move", key="stage_move_btn"):
                import inspect
                try:
                    if "axis" in inspect.signature(stg.move_to).parameters:
                        ax = int(st.session_state.get("stage_axis", 1))
                        stg.move_to(float(stage_target), axis=ax)
                    else:
                        stg.move_to(float(stage_target))
                    st.success(f"Moved to {stage_target}")
                except Exception as e:
                    st.error(f"Move Fail: {e}")
        else:
            st.warning("No Stage connected")

    # C) Rotation quick test (full width). Uses adapter's internal active_axis.
    st.markdown("**Rotation Quick Test**")
    rot_options = []
    for k in ("rotation", "rotation1", "rotation2"):
        if k in devices:
            rot_options.append(k)

    if not rot_options:
        st.warning("No rotation mount connected.")
    else:
        which_rot = st.selectbox("Choose mount", rot_options, key="rot_choice_manual")
        rot_target = st.number_input("Angle (deg)", value=0.0, step=1.0, key="rot_target_manual")
        btn_go, btn_read, btn_zero = st.columns(3)

        with btn_go:
            if st.button("Go", key="rot_go_manual", use_container_width=True):
                try:
                    dev = devices[which_rot]
                    if hasattr(dev, "is_connected") and not dev.is_connected():
                        if hasattr(dev, "connect"):
                            dev.connect()
                    dev.move_to(float(rot_target))  # adapter uses its active axis internally
                    st.success(f"{which_rot}: moved to {rot_target:.2f}°")
                except Exception as e:
                    st.error(f"{which_rot}: Move failed: {e}")

        with btn_read:
            if st.button("Read", key="rot_read_manual", use_container_width=True):
                try:
                    dev = devices[which_rot]
                    if hasattr(dev, "is_connected") and not dev.is_connected():
                        if hasattr(dev, "connect"):
                            dev.connect()
                    pos = dev.get_position()  # adapter uses its active axis internally
                    st.info(f"{which_rot}: {pos:.3f}°")
                except Exception as e:
                    st.error(f"{which_rot}: Read failed: {e}")

        with btn_zero:
            if st.button("Zero", key="rot_zero_manual", use_container_width=True):
                try:
                    dev = devices[which_rot]
                    if hasattr(dev, "is_connected") and not dev.is_connected():
                        if hasattr(dev, "connect"):
                            dev.connect()
                    dev.move_to(0.0)
                    st.success(f"{which_rot}: moved to 0.00°")
                except Exception as e:
                    st.error(f"{which_rot}: Zero failed: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN CONTENT (Presets / MegaSweep)
# ──────────────────────────────────────────────────────────────────────────────

# Compose devices_main for the views (spectrometer + IV + aux)
devices_main = {}
spec = st.session_state.get("spec")
if spec is not None and st.session_state.get("lf6_ready"):
    devices_main["spectrometer"] = spec
if "iv" in locals() and isinstance(devices.get("iv"), IVDevice):
    devices_main["iv"] = devices["iv"]

# Bring over aux devices (if connected)
for k in ("stage", "pm", "rotation", "rotation1", "rotation2"):
    if k in devices:
        devices_main[k] = devices[k]

# Wavelength headers for CSVs (only if spectrometer present)
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

# Navigation / routing
st.sidebar.markdown("---")
st.sidebar.title("Experiment Selection")

app_mode = st.sidebar.radio(
    "Select Mode",
    ["Dual Gate Sweep", "MegaSweep"]
)

if app_mode == "Dual Gate Sweep":
    render_presets(devices_main, wavelength_headers, extra_scalar_fields_order)
elif app_mode == "MegaSweep":
    render_megasweep(devices_main)
