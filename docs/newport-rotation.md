# Newport ESP300 / ESP301 rotation

RotIn and RotOut each support **Newport ESP300 (shared)** and **Newport
ESP301 (shared)**. A three-axis controller can supply separate axes to the
two roles. Two independent one-axis controllers can each use axis 1.
The same controller/axis cannot be assigned to both rotation roles.

1. Select the model and VISA address in the relevant rotation section.
2. For ESP301 serial connections, choose **USB · 921600 baud** for native
   USB or the matching **RS-232** baud for a serial cable (default 19200).
   This setting changes the PC connection, not the controller configuration.
3. Click **Scan Axes**. Only detected usable axes appear. The previous axis
   remains selected if present; a one-axis result selects that axis, including
   for RotOut. An empty/failed scan never creates an artificial axis 1.
4. Select an axis and connect. Connection validates the axis, applies the
   existing per-axis motion profile, then enables that motor automatically.
   For axis 1 this sends `1MO`, then reads `1MO?`; connection succeeds only
   after the power readback is `1`. The status displays **Motor ON · verified**.
   Only the selected axis is enabled. Scanning itself
   does not enable motors, move stages, home, or save controller settings.

Scan and connection work run outside the GUI thread. Mapping controls stay
locked while an operation is pending or a mount is connected. Disconnect
before rescanning or changing the mapping. Shared controller sessions remain
open until the last rotation/stage user releases them; conflicting serial
baud settings are rejected.

## USB controller missing from the port list

An **ESP301 Motion Control** device under **Other devices** with Windows
**Code 28** has no installed USB driver. It cannot yet be opened as a COM/VISA
resource. Its hardware ID is `USB\VID_104D&PID_3001`.

Use the official driver from the
[Newport ESP301 product page](https://www.newport.com/p/ESP301-3G), following
the [USB driver installation instructions](https://www.newport.com/medias/sys_master/npresources/hb5/h6a/9954466168862/ESP301%20-%20USB%20Driver%20Installation%20Manual/ESP301-USB-Driver-Installation-Manual.pdf).
In Device Manager, choose **Update driver → Browse my computer** and select
the matching driver folder. After successful installation, the device should
appear under **Ports (COM & LPT)**. Refresh the app's address list and scan
that port. Do not select an unrelated COM port just because its number
matches an older saved configuration.

The official package is labelled for Windows 10; loading on other Windows
versions must be confirmed on the actual system. Do not substitute an unrelated
generic driver or disable signature checks to make the port appear.

This integration changes Python controller/driver code and requires an app
restart to load. Refreshing a measurement-page layout does not replace live
instrument controllers.

Protocol reference: [ESP301 user manual](https://www.newport.com/medias/sys_master/images/images/hda/h3e/9117547069470/ESP301-User-s-Manual.pdf),
including native `VE?` identity and `ZU` driver/stage discovery. `ZU` reports
installed driver bits separately from attached ESP-compatible stage bits;
the app lists axes with both present. Older unsupported `ZU` implementations
use a bounded, read-only `QM?` / `ID?` fallback.

Live checks found `ZU = 0H` on both an **ESP301 1.1.1** with an SR50CC
and an **ESP300 3.09** with a URS75CC, even though their axis-1 motor/stage
queries succeeded. Consequently, an all-zero bitmap is not enough to rule out
usable axes. The app first verifies the native `VE?` identity against the
selected controller model, then requires both a configured motor and a valid
stage ID for each detected axis. An unknown/mismatched controller, an unknown
stage ID, or a saved axis number alone never establishes stage presence.
Nonzero bitmaps retain their existing driver/stage interpretation.

## Connected but the stage does not move

`MD? = 1` only means the axis is idle/done; it does not prove that a move was
accepted or the target was reached. The app verifies motor power after the
connection's enable command and before sending a position command. Loss of
motor power during a move is a hardware fault, so it stops corrective moves
instead of repeatedly sending the same target.

The controller's error FIFO is read only when diagnosing a motor-off fault.
Read entries are retained for diagnostics, including entries belonging to
other axes on a shared controller. The visible error identifies the selected
axis; the app does not automatically re-enable a faulted motor or change its
following-error threshold, speed, travel limits, or protection configuration.

For example, **103 — AXIS-1 FOLLOWING ERROR THRESHOLD EXCEEDED** means the
controller's desired position exceeded the permitted difference from measured
position. The controller can then disable the motor; subsequent commands
produce **113 — AXIS-1 MOTOR NOT ENABLED**. The latter is a consequence,
not the original fault. Check the motor/encoder connection, mechanical load,
and the controller's stage/servo configuration before another motion test.
These error codes alone cannot distinguish which of those caused the fault.
Do not increase the following-error threshold merely to suppress the error.

Software limits must be interpreted together with the `ZS?` enable flags;
`SL? = SR? = 0` alone does not establish an active zero-width travel range.
