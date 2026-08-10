# NextGen Stand — bench prototyping

Teensy 4.1 firmware and a Python host for bench work: valves, a flow meter, a
pump, and a live console to drive them.

```
firmware/          C firmware for the Teensy 4.1
  lib/ngs/           protocol, framing, dispatch — pure C, no hardware
  src/main.cpp       Arduino entry point + the hardware implementation
  test/test_ngs/     Unity tests, run on the board
host/ngs_host/     Python host package
host/tests/        pytest suite (no hardware needed)
platformio.ini     firmware build config
```

## The bench as wired

| What | Command | Pin | Notes |
|---|---|---|---|
| Valve 1 | `V1` | digital 32 | HIGH opens |
| Valve 2 | `V2` | digital 31 | HIGH opens |
| Flow meter | `F` | analog 27 (A13) | 4–20 mA → 0.6–3.0 V → 0–600 mL/min |
| Pump | `P` | PWM 33 | 50 kHz, 12-bit, 0–100 % |

That table lives in code as `BENCH_CONFIG` in
[bench.py](host/ngs_host/bench.py) — it is the only place the wiring is
written down, and the dashboard, the command language, and the CLI all derive
from it.

## Setup

The venv at `.venv` holds both PlatformIO and the host package.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Firmware

```powershell
.\.venv\Scripts\pio.exe run                # build
.\.venv\Scripts\pio.exe run -t upload      # flash a connected board
.\.venv\Scripts\pio.exe test -e teensy41   # run the C tests ON the board
```

The C tests run on-target rather than natively: there is no host C compiler in
this toolchain, and the real Cortex-M7 also catches the alignment and
endianness assumptions an x86 build would paper over.

## First run on hardware

1. **Plug the Teensy in** and confirm the host sees it:

   ```powershell
   .\.venv\Scripts\ngs.exe ports
   ```

   No output means Windows has not enumerated it — try a different cable
   (charge-only USB cables are the usual culprit) before suspecting the board.

2. **Flash it.** The board must be in bootloader mode; press the button on the
   Teensy if the upload does not start on its own.

   ```powershell
   .\.venv\Scripts\pio.exe run -t upload
   ```

   The port disappears and comes back a second or two later. The LED blinks at
   1 Hz once the firmware is running — that is the idle heartbeat, and it goes
   to a fast flutter while telemetry is streaming.

3. **Check it, without moving anything.** Read-only: identifies the firmware,
   measures round-trip time, reads the flow meter.

   ```powershell
   .\.venv\Scripts\ngs.exe check
   ```

   Expect a sub-millisecond round trip and a flow reading at 0.6 V with the
   pump off. A `FAULT` on the flow channel means under 4 mA — sensor
   unpowered, loop open, or the wrong sense resistor.

4. **Exercise the outputs** once the readings look sane. This moves real
   hardware: it cycles each valve, then opens the flow path and ramps the pump
   to 25 % and 50 % before returning everything to the safe state.

   ```powershell
   .\.venv\Scripts\ngs.exe check --outputs
   ```

5. **Drive it.**

   ```powershell
   .\.venv\Scripts\ngs.exe bench
   ```

If something looks wrong, `ngs status` shows the link counters and
`ngs raw GET_STATUS` talks to the board with everything above the protocol
layer out of the way.

### Before trusting the numbers

Two things this repo assumes and cannot check for itself:

- **HIGH opens the valves.** If the drivers invert, set `open_level=0` on the
  `ValveSpec` — one field, no other changes.
- **The flow sensor's 4–20 mA maps to 0.6–3.0 V**, i.e. a 150 Ω sense
  resistor. `ngs check` reports raw volts alongside the scaled value, so a
  wrong resistor shows up as a reading that will not reach 600 mL/min at
  full scale.

The dashboard reads valve pins back every poll and compares them with what it
commanded. A red `OUTPUT MISMATCH` banner means the board reset underneath you
— a brownout when a solenoid kicks in is the usual cause — and the outputs are
no longer where you left them. Press `Z` to re-apply the safe state.

## Host

```powershell
.\.venv\Scripts\ngs.exe bench          # live dashboard, 2 Hz
.\.venv\Scripts\ngs.exe web            # the same dashboard in a browser window
.\.venv\Scripts\ngs.exe bench --sim    # either one with no board attached
.\.venv\Scripts\ngs.exe send "V1O;P50;"
.\.venv\Scripts\ngs.exe ports          # find attached Teensys
.\.venv\Scripts\ngs.exe check          # bring-up check, read-only
.\.venv\Scripts\ngs.exe selftest       # verify the board before wiring it up
```

Only one process can hold a serial port on Windows, so the terminal dashboard,
the web dashboard and the one-shot commands cannot run at the same time. Quit
one before starting another.

### In a separate window

`ngs web` serves the dashboard at `http://127.0.0.1:8765/` and opens a browser
— pop it out, put it on a second monitor, leave it up while you work in the
terminal. Same bench, same command language, same 2 Hz poll; it binds to
localhost only, because it drives real hardware.

`--sim` works on every command. It swaps the serial port for a simulated
device with a crude flow model behind it, which is how the UI gets exercised
when the hardware is busy.

### The dashboard

`ngs bench` polls the board at 2 Hz and takes commands on the same screen:

```
+- NextGen Stand bench ------------------------------------------------------+
| COM7  fw 0.1.0  2.0 Hz  up 41.2s  rx 184  tx 184  crc-err 0  43.1 C        |
|                                                                            |
| P    Pump speed      50.0 %    pin 33, 50 kHz, 12-bit                      |
| V1   Valve 1         OPEN      pin 32                                      |
| V2   Valve 2         CLOSED    pin 31                                      |
| F    Flow meter      298.4 mL/min   pin 27, 1.794 V, raw 2226              |
|                                                                            |
| > P60_                                                                     |
+---------------------------------- ? for help, Q to quit -------------------+
```

The header is measured, not assumed — if the link slows down, the displayed
rate drops rather than claiming 2 Hz over stale numbers.

### Commands

| Command | Effect |
|---|---|
| `V1O` `V1C` `V1T` `V1?` | valve 1 open / close / toggle / query |
| `P50` `P37.5` | pump setpoint, percent |
| `P+5` `P-5` | nudge the setpoint |
| `F?` | read the flow meter |
| `S` | device status counters |
| `X` | stop: pump to 0, valves closed |
| `Z` | re-initialise to the safe state |
| `Q` | quit |

Chain with `;` — `V1O;V2C;P50;`. Case and whitespace do not matter, and the
trailing `;` is optional. A failure stops the rest of the line rather than
applying half an operator's intent.

### As a library

```python
from ngs_host import Bench, Device

with Device.open() as dev:
    bench = Bench(dev)
    bench.initialize()
    bench.set_valve("valve1", True)
    bench.set_pwm("pump", 40.0)
    print(bench.read_analog("flow").value, "mL/min")
```

## Adding hardware

Adding another valve, sensor, or PWM output is a config change, not a firmware
change — the firmware speaks pins and ADC counts, and the bench layer supplies
the meaning. Add a spec to `BENCH_CONFIG`:

```python
ValveSpec(name="drain", code="V3", pin=30, description="Drain valve"),
```

It is immediately typeable (`V3O`), shows up in `?` help and on the dashboard,
and gets a CLI entry. Run `pytest` — `BENCH_CONFIG.validate()` checks the pin
against what the board can actually do (PWM peripheral present, pin is an
analog input, frequency achievable at that resolution).

Firmware changes are only needed for genuinely new *capabilities* — a new bus,
a new measurement mode — not for new instances of what already exists.

## Protocol

`0x00 <COBS( type seq len payload crc16 )> 0x00` over USB CDC. COBS removes
every zero byte from the body, so a lone `0x00` is an unambiguous frame
delimiter and either side can resynchronise after a dropped byte or a reset
mid-frame. CRC-16/CCITT-FALSE covers the header and payload.

[`firmware/lib/ngs/ngs_protocol.h`](firmware/lib/ngs/ngs_protocol.h) is the
single source of truth. [`protocol.py`](host/ngs_host/protocol.py) mirrors it,
and `host/tests/test_protocol_sync.py` parses the header and fails if the two
drift — including a message added to the firmware and forgotten on the host.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest             # host, no hardware needed
.\.venv\Scripts\python.exe -m ruff check host\
.\.venv\Scripts\pio.exe test -e teensy41         # firmware, needs the board
```

The host suite runs against `fake.py`, which reimplements the firmware's
observable behaviour — same responses, same error codes for the same bad
input. It is not a substitute for the on-target tests: those check the C
itself, this checks that the host agrees with what the C is specified to do.
