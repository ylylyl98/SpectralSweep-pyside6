import streamlit as st
from app.ui_streamlit.views.presets import render as render_presets
from app.devices.lf6_adapter import SpectrometerLF6
from app.devices.iv_adapter import IVDevice
import lf6_automation, iv_automation

st.set_page_config(page_title="Lab Runner (Streamlit - Real Devices)", layout="wide")

st.sidebar.title("Device Configuration")

# ----- LF6 experiment & exposure -----
with st.sidebar.expander("LF6 Spectrometer", expanded=True):
    try:
        # Discover saved experiments
        exp_names = []
        try:
            # Use LightField API on the underlying setup before wrapping
            lf6_raw = lf6_automation.LF6Setup()
            try:
                exp_iter = lf6_raw.experiment.GetSavedExperiments()
                exp_names = list(exp_iter) if exp_iter is not None else []
            except Exception:
                exp_names = []
        except Exception as e:
            st.error(f"LF6Setup initialization failed: {e}")
            st.stop()

        if not exp_names:
            st.info("No saved experiments found via API. You can still proceed if your default experiment loads.")

        exp_name = st.selectbox("Saved Experiment", options=(exp_names or ["<Use default/current>"]))
        exposure_ms = st.number_input("Exposure (ms)", min_value=1.0, max_value=600000.0, value=2000.0, step=10.0)

        # Initialize LF6 with selection
        lf6 = lf6_raw
        if exp_name and exp_name != "<Use default/current>":
            try:
                lf6.load_experiment(exp_name)
                st.success(f"Loaded experiment: {exp_name}")
            except Exception as e:
                st.warning(f"Failed to load experiment '{exp_name}': {e}")

        # Apply exposure time if supported
        try:
            if hasattr(lf6, "change_expose_time"):
                lf6.change_expose_time(exposure_ms)
                st.caption("Exposure applied.")
        except Exception as e:
            st.warning(f"Could not set exposure: {e}")

        # Wrap as app spectrometer
        spec = SpectrometerLF6(lf6)
        st.sidebar.success("LF6 ready.")
    except Exception as e:
        st.sidebar.error(f"LF6 init failed: {e}")
        st.stop()

# ----- IV VISA discovery -----
with st.sidebar.expander("IV Instruments (VISA)", expanded=False):
    import pyvisa

    rm = None
    resources = []
    try:
        rm = pyvisa.ResourceManager()
        resources = list(rm.list_resources())
    except Exception as e:
        st.warning(f"PyVISA not available: {e}")

    st.caption("Select VISA resources to include in IV setup.")
    selected = st.multiselect("Resources", options=resources, default=[])
    term = st.text_input("Termination", value="\n", help="Write/read line termination")

    # Test VISA connections with *IDN?
    if st.button("Test selected VISA (*IDN?)"):
        if not selected:
            st.info("Select at least one VISA resource to test.")
        else:
            results = []
            try:
                for addr in selected:
                    try:
                        res = rm.open_resource(addr)
                        res.read_termination = term
                        res.write_termination = term
                        res.timeout = 3000  # ms
                        try:
                            res.write("*IDN?")
                            idn = res.read().strip()
                        except Exception:
                            # Some instruments prefer query()
                            idn = res.query("*IDN?").strip()
                        results.append((addr, "OK", idn))
                        try:
                            res.close()
                        except Exception:
                            pass
                    except Exception as e:
                        results.append((addr, "ERROR", str(e)))
            except Exception as e:
                st.warning(f"VISA test encountered an error: {e}")
            if results:
                st.caption("VISA *IDN? results")
                for addr, status, info in results:
                    if status == "OK":
                        st.success(f"{addr} → {info}")
                    else:
                        st.error(f"{addr} → {info}")

    # Optional: nicknames
    nick_inputs = {}
    for addr in selected:
        nick_inputs[addr] = st.text_input(f"Name for {addr}", value=addr)

    # Build IVSetup from selections
    devices = {"spectrometer": spec}
    try:
        if selected:
            inst_list = []
            from iv_automation import PyvisaInstrument, IVSetup
            for addr in selected:
                name = nick_inputs[addr] or addr
                inst = PyvisaInstrument(address=addr, name=name, termination=term, rm=rm)
                inst_list.append(inst)
            iv_setup = IVSetup(inst_list)
            iv = IVDevice(iv_setup)
            devices["iv"] = iv
            st.success(f"IV ready with {len(inst_list)} instrument(s).")
        else:
            st.info("No VISA devices selected. Leakage columns will be zeros.")
    except Exception as e:
        st.warning(f"IV init failed: {e}")
        # keep spectrometer-only
        devices = {"spectrometer": spec}

# Wavelength headers & extra scalar columns
wavelength_headers = spec.calibration_wavelengths()
if not wavelength_headers:
    st.error("No wavelength calibration found from LF6. Ensure experiment is loaded and camera is connected.")
    st.stop()

# Add any extra scalar columns here (appear before wavelengths)
extra_scalar_fields_order = []  # e.g., ["temperature_K","theta_deg"]

# Main view
render_presets(devices, wavelength_headers, extra_scalar_fields_order)
