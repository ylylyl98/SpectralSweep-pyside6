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

## 🛠️ Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/ylylyl98/lab_runner_streamlit.git
    cd lab_runner_streamlit
    ```

2.  **Switch to the Stable Branch:**
    **Important:** Please ensure you are using the stable release for all experiments.
    ```bash
    git checkout stable
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

    ## 🚀 Quick Start

1.  **Run the App:**
    ```bash
    double click run_stable.cmd
    ```
    *Note: This script automatically syncs your code to the `stable` branch to ensure reliability. Do not use `run_dev.cmd` for actual experiments.*

2.  **Usage:**
    * Use the **Sidebar** to connect devices and select your mode ("Dual Gate" or "MegaSweep").
    * Set your **Voltage Limits** and **Sample Name** before starting.
