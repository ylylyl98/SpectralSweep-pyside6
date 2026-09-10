# ESP32 imaging stage trial

The sample stage is open loop. It has no encoder and no home sensor, so the
application displays an estimated distance from a manually confirmed imaging
end. The application requires the `stage-v2` firmware handshake before it
enables movement, reference, or device settings. Legacy `stage-v1` firmware is
shown as an update-required condition and its coordinate is never presented as
the imaging-end coordinate.

The current driver is 200 pulses/revolution and the documented T6x1 lead screw
therefore gives 200 steps/mm. The firmware supports 200, 800, 1600, and 3200
pulses/revolution. It reports the active scale, frequency, and explicit
electrical direction mapping. The direction mapping says which GPIO level
moves away from the imaging end; it is a configuration declaration, not a
hardware verification.

## Daily workflow

Select a serial port and connect. The first reference operation is manual:
position the sample at the right imaging reference, stop, then expand
**Advanced setup** and press **Set imaging reference…**. Confirm
**Set current position to 0** only after checking the physical position.
Opening the dialog or cancelling it does not change zero; the dialog defaults
to Cancel. The app cannot detect contact with or verify
force at a mechanical stop; it performs no automatic overtravel or homing. The left control,
**← Away from imaging end**, sends positive distance. The right control,
**Toward imaging end →**, sends negative distance. Fine, Medium, and Coarse
presets default to 0.01, 0.1, and 1 mm. Slow and Normal default to 100 and 500
Hz; applying a speed sends one explicit frequency command and waits for its
firmware acknowledgement.

Once the stage is referenced and a safe maximum is saved, the daily panel also
offers **Go to imaging reference (0) →** and **← Go to saved maximum**. These
are absolute moves within the saved range. The **Advanced setup** panel has a
**Move to target** field and explicit Move button for another absolute
destination; values outside the displayed 0-to-maximum range are disabled.
The displayed position is an open-loop estimate and does not guarantee physical
seating at either endpoint.

An unreferenced stage permits only supervised finite jogs of at most 10 mm and
has no absolute travel protection. After reference, movement is permitted only
between 0 and the saved maximum. If a reference exists but the maximum is
unset, the stage refuses away movement until a conservative maximum is applied
in Advanced. The maximum setter accepts 0 < mm <= 100 and cannot be below the
current referenced distance; **Save current as maximum** requires a positive
current position. Never use a jog as an automatic search for a hardstop.

STOP is available whenever connected. It sends the immediate stop byte,
invalidates the position estimate, and requires manual reseating and a new
zero. **Clear reference** preserves the saved maximum and also requires manual
reseating. There is no automatic home or overshoot operation.

## Advanced setup

Advanced is collapsed by default. It contains the numeric maximum, editable
custom Fine/Medium/Coarse and Slow/Normal values through the persisted app
configuration, the GPIO direction declaration, the 200/800/1600/3200 driver
scale selector, and explicit Apply buttons. Scale and direction changes are
accepted only while idle. Firmware persists scale, direction, frequency, and
maximum together in one versioned configuration blob; changing scale or
direction clears both the saved maximum and zero. The app updates its defaults
from firmware readback and never writes firmware during session restoration.

The host keeps a 500 ms status heartbeat, rejects stale status after 1.5 s,
leaves a 350 ms quiet interval after stopping or completing a move (exceeding
the firmware's 250 ms requirement), and never queues deferred
movement or retries a command whose acknowledgement was lost. Firmware stops
motion after approximately two seconds without communication. STOP is a
software stop and does not replace a hardware emergency stop.

## Trial checks

Before testing, build and upload `firmware/esp32_stage` with PlatformIO and
verify the reported scale against the physical driver switch. Close any serial
monitor before connecting the app. With clearance, verify both directions at
0.1 mm, establish zero, apply a conservative maximum, and refine it only with
clearance. Test STOP during a short move. Record connection result, direction,
approximate travel, acknowledgement behavior, STOP response, and repeatability.

On 2026-09-10, PlatformIO 6.2.0 and the Espressif toolchain were installed locally.
The firmware built successfully with espressif32 7.1.2 and was uploaded to the
ESP32-S3 on COM4. Esptool verified the written flash hashes. Three status-only
readbacks through the app's protocol adapter confirmed stage-v2, 200 pulses/mm,
100 Hz, idle motion, an unreferenced position, and no saved maximum. The serial
port was closed afterward so SpectralSweep can reconnect. No movement commands
were sent; physical direction and travel checks remain to be performed.

The upload record and source/binary hashes are stored in
`commissioning_evidence/esp32_stage_v2_upload_20260910.json`. The Python protocol,
controller, UI, and related application regressions were tested with simulated
hardware, and the panel was visually checked at a 350 px sidebar width.
