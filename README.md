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
| COM3  fw 0.1.0  2.0 Hz  up 41.2s  rx 184  tx 184  crc-err 0  43.1 C        |
|                                                                            |
| P    Pump speed      50.0 %         pin 33, 50 kHz, 12-bit                 |
| V1   Valve 1         OPEN           pin 32                                 |
| V2   Valve 2         CLOSED         pin 31                                 |
| F    Flow meter      298.4 mL/min   pin 27, 1.794 V, raw 2226              |
|                                                                            |
| +- commands ------------------------------------------------------------+  |
| | V1O / V1C / V1T / V1?  Valve 1 (pin 32)   T<setpoint>  autotune       |  |
| | V2O / V2C / V2T / V2?  Valve 2 (pin 31)   T? / TA / TX tune: see/adopt|  |
| | VO / VC                all valves         ! or E       EMERGENCY STOP |  |
| | P<0-100> / P+<n> / P?  Pump speed %       EC           clear E-stop   |  |
| | PA<setpoint> / PM      closed loop        S            device status  |  |
| | F?                     read flow          X            soft stop      |  |
| | K? / KP<n> / KI<n>     loop gains         Z            re-initialise  |  |
| | KF<seconds> / KB<n>    filter / deadband  Q            quit           |  |
| +------------------------------------------------------------------------+ |
|                                                                            |
| V1 Valve 1: OPEN                                                           |
| > P60_                                                                     |
+------- Ctrl-E: EMERGENCY STOP  .  chain with ';'  .  Q to quit ------------+
```

The command reference is a permanent panel, not something printed into the log
-- the log is a stream and the reference is not, so mixing them meant the list
scrolled away the moment anything happened. It shrinks to a syntax-only,
three-wide layout on a short terminal, and the log gives up its lines before
the reference gives up any of its. Below about 24 rows the window is genuinely
too small; enlarge it.

`ngs web` puts the same list in a sticky side dock at a smaller size.

The header is measured, not assumed — if the link slows down, the displayed
rate drops rather than claiming 2 Hz over stale numbers.

### Commands

| Command | Effect |
|---|---|
| `V1O` `V1C` `V1T` `V1?` | valve 1 open / close / toggle / query |
| `VO` `VC` | open / close **every** valve at once |
| `P50` `P37.5` | pump duty, percent (manual mode) |
| `P+5` `P-5` | nudge the duty |
| `PA250` | closed loop: hold 250 mL/min |
| `PM` | back to manual, holding the current duty |
| `K?` `KP0.16` `KI0.02` `KD0` | show / set the loop gains |
| `KF1.5` `KB2` | measurement filter seconds / integration deadband |
| `T240` `T?` `TA` `TX` | autotune / progress / adopt gains / abort |
| `F?` | read the flow meter |
| `!` or `E` | **EMERGENCY STOP** — everything safe, latched (`Ctrl-E` needs no Enter) |
| `EC` | clear the emergency stop |
| `S` | device status counters |
| `X` | soft stop: pump to 0, valves closed, not latched |
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

## Emergency stop

`Ctrl-E` in the terminal dashboard, the big red button (or `Escape`) in the
browser, `!` as a command, `ngs estop` from a shell. All four do the same
thing:

```
!        everything to its safe state, latched
EC       clear the latch
```

Three properties make it a stop rather than a convenience command:

**It is one message, and the device does the work.** The board holds its own
safe-state table — registered by the host at connect, because the device has
no idea what a valve is — and drives every output itself. A sequence of
individual commands can half-succeed; this cannot.

**It latches.** While engaged, every command that would drive an output is
refused with `ESTOP`: GPIO writes, PWM writes, entering auto, starting an
autotune. Reads keep working, because a stopped bench is exactly when you want
to see what it is doing. Returning the loop to manual is also allowed — that is
a way *out* of driving something.

Clearing the latch moves nothing. Outputs stay at their safe values until
something is explicitly commanded again.

**It does not need the host.** With the watchdog enabled, the device latches by
itself if the host stops talking for longer than the timeout — the case a
host-side stop can never cover, because by then the host is the thing that
failed. The dashboards switch it on while they are supervising:

```powershell
.\.venv\Scripts
gs.exe bench --watchdog 3000    # latch after 3 s of silence
```

It is **off by default**, and deliberately so: a watchdog that fires whenever
no host is connected would also undo a valve you set from a one-shot command
and then walked away from, which is ordinary bench use rather than an
emergency. Set `watchdog_ms` in `BENCH_CONFIG` to make it always-on.

## Closed-loop flow control

The pump runs in one of two modes. **Manual** is a duty cycle you set. **Auto**
holds a flow setpoint in mL/min, with the loop running *on the Teensy* at
50 Hz — a control loop paced by USB round trips is at the mercy of host
scheduling, which is the one thing a controller must not have.

```
PA250;      hold 250 mL/min
PM;         back to manual, keeping the duty the loop had reached
```

The transfer is bumpless in both directions: entering auto seeds the
integrator from the duty already applied and starts the setpoint ramp from the
flow actually measured, so the pump does not step when the mode changes.

### Dealing with a noisy flow signal

Three things, in the order the signal meets them:

1. **Median of 5** on the raw samples, which removes isolated spikes outright.
   Ahead of the low-pass on purpose — an IIR filter smears a spike across its
   whole time constant instead of removing it.
2. **First-order low-pass**, `KF<seconds>`, default 1 s. The loop is allowed to
   be slow; a few seconds to correct is fine and stability is worth more.
3. **Deadband**, `KB<mL/min>`, default 2. Inside it the error is mostly noise,
   so the integrator holds rather than walking the output around forever.

Derivative gain defaults to **zero**. On a noisy flow signal D amplifies noise
far more reliably than it improves response. It is configurable if you want it.

### Setpoint changes and windup

A step change is *ramped* (`setpoint_slew`, default 60 mL/min/s) rather than
applied instantly, so the loop follows a trajectory it can actually track
instead of an error step that saturates the output and charges the integrator.
The output has its own rate limit too.

Windup is handled twice over: the integrator only accumulates when doing so
would not drive further into a limit, *and* the accumulated term is clamped to
the output range. Conditional integration alone still lets the term sit pinned
long after the error reverses.

The derivative acts on the measurement, not the error, so a setpoint step
produces no derivative kick.

### Autotune

```powershell
.\.venv\Scripts
gs.exe tune 240 --adopt
```

Relay feedback (Åström–Hägglund): the output is driven up and down around the
setpoint until the flow settles into a limit cycle, whose amplitude and period
give the ultimate gain and period directly — no process model needed, and the
loop is never deliberately pushed unstable. The hysteresis band keeps the relay
switching on the process rather than on sensor noise.

Gains come from **Tyreus–Luyben** by default, which is markedly less aggressive
than Ziegler–Nichols (`--rule ziegler-nichols`, or `pessen` for faster and less
damped). ZN was derived for quarter-amplitude decay, i.e. for a loop that
visibly rings; on a bench pump, settling slowly beats oscillating.

It refuses rather than guessing when the experiment was not informative — a
swing that barely clears the hysteresis band, a period only a few control ticks
long, or cycles too scattered to average. Those cases do not give a slightly
uncertain gain, they give a confidently enormous one, because Ku divides by
`sqrt(a² - h²)`.

**The pump oscillates on purpose during a tune.** Have the flow path open, and
the command aborts the experiment and stops the pump if it exits for any
reason.

### Tuning is saved

Gains, filter and deadband persist in `tuning.json` at the repo root, keyed by
the board's MCU serial. They load automatically on every connect, so an
autotune survives closing the terminal — and a second Teensy on the bench does
not inherit the first one's tuning.

```powershell
.\.venv\Scripts
gs.exe gains                  # show them, and where they came from
.\.venv\Scripts
gs.exe gains --kp 0.16        # set and save
.\.venv\Scripts
gs.exe gains --reset          # discard, back to BENCH_CONFIG
```

The file is meant to be committed: it diffs, so "kp went from 0.16 to 0.31 on
the 12th, from an autotune with Ku 0.52" is a question git can answer. An
autotune records what it measured alongside the gains it produced.

Setpoint and mode are deliberately *not* saved. Restoring those would mean
opening a terminal could start the pump, which is not something a
configuration file should be able to do — and it keeps the file quiet while
you work, so running the bench produces no diff.

It lives on the host rather than in the board's EEPROM because tuning is bench
configuration, and in this project that lives under version control next to
the calibration it depends on. Gains are only meaningful against a particular
pump, line and flow meter. If you ever want the board to hold its own gains so
it can run standalone, that is a firmware change and worth asking for.

### If the sensor dies mid-run

A reading below `fault_below` (default −25 mL/min, i.e. under 4 mA) means the
loop is open, not that the flow is low. Chasing it would ramp the pump to full
against a sensor that cannot report back, so the controller drops to manual at
the safe output, latches a fault counter, and makes you re-enable it.

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
