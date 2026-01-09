# 🧪 Lab Runner Streamlit

**Lab Runner** is a dashboard for automating physics optical experiments with LightField software from Princeton Instruments. It lets you control Keithley 2400/2401 SMUs, manage voltage sweeps, and collect spectroscopic data in a single interface.

## ⚡ What It Does

* **Unified Control:** Manage Spectrometers, CCD cameras, Motion Stages (e.g., for ND filter control), Rotators (e.g., for waveplate angles), and SMUs from one screen.
* **Dual Gate Sweep:** Run loops either Nested or Synchronized (setting center wavelength, exposure times, motion stage positions, rotator angles) and complex sequences (TG ± BG) with automatic safety checks.
* **MegaSweep:** Perform advanced high-dimensional voltage mapping (Vtg stripes vs. Vbg) with "Snake" routing for efficiency.

## 🔌 Supported Hardware

* **Spectrometers:** Princeton Instruments LightField.
* **Electronics (IV):** VISA-compatible Keithley 2400/2401.
* **Motion:** Thorlabs Elliptec & Newport ESP300 (Stages and Rotators).
* **Power:** Thorlabs PM100D Power Meters.

## 🚀 Quick Start

1.  **Install requirements:**
    ```bash
    pip install -r requirements.txt
    ```

2.  **Run the App:**
    ```bash
    double click run_ui.cmd
    ```

3.  **Usage:**
    * Use the **Sidebar** to connect devices and select your mode ("Dual Gate" or "MegaSweep").
    * Set your **Voltage Limits** and **Sample Name** before starting.
