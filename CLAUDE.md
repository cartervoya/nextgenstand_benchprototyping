# Working in this repo

Teensy 4.1 bench firmware (C) plus a Python host. See [README.md](README.md)
for what the bench is and how to run things; this file is about how to change
it without breaking the parts that are load-bearing.

## Commands

Everything runs through the project venv — PlatformIO is installed there, not
globally.

```powershell
.\.venv\Scripts\pio.exe run                            # build firmware
.\.venv\Scripts\pio.exe run -t upload                  # flash (needs the board)
.\.venv\Scripts\pio.exe test -e teensy41               # C tests, on the board
.\.venv\Scripts\python.exe -m pytest                   # host tests, no hardware
.\.venv\Scripts\python.exe -m ruff check host\
.\.venv\Scripts\ngs.exe bench --sim                    # dashboard, no hardware
```

Do not add a `native` PlatformIO env: this machine has no host C compiler,
which is why the C tests run on-target.

`pio test` sometimes reports **`[PASSED]` with 0 test cases** — that is not a
pass, it means it could not open the port to read the results (the board is
still re-enumerating after the upload). Always check the case count. If it
happens, split the run:

```powershell
.\.venv\Scripts\pio.exe test -e teensy41 --without-testing
.\.venv\Scripts\pio.exe test -e teensy41 --without-building --without-uploading --test-port COM3
```

## The layering, and why it matters

```
firmware/lib/ngs/     pure C: protocol, framing, dispatch. No hardware calls.
firmware/src/main.cpp the only C++, and the only place hardware is touched.
host/ngs_host/        protocol -> link -> device -> bench -> commands -> ui
```

Two seams carry most of the design:

**`ngs_board.h`** is the only path from the C application layer to the Arduino
world. `ngs_app.c` calls nothing else, which is what lets the tests link the
real dispatch code against a fake board. If you find yourself writing an
algorithm in `main.cpp`, it belongs in `ngs_app.c` behind a new `ngs_board_*`
call.

**`bench.py`** is the only place that knows what the hardware *means*.
Everything below it speaks pins and ADC counts. Adding a valve or a sensor is
a `BENCH_CONFIG` entry, not a firmware change — the dashboard, command
language and CLI all derive from that config. Firmware changes are for new
capabilities, not new instances.

## The protocol is shared, so change both sides together

`firmware/lib/ngs/ngs_protocol.h` is the single source of truth.
`host/ngs_host/protocol.py` mirrors it. `host/tests/test_protocol_sync.py`
parses the header and compares every constant and struct field, so drift fails
the host test run — but only after you have already written the C. Update the
mirror in the same commit.

Bump `NGS_PROTO_VERSION` on any incompatible change to framing, message ids,
or struct layout. The host checks it on connect and refuses a mismatch.

Payload structs are packed, little-endian, fixed-width types only, with
explicit `_pad` fields keeping things naturally aligned. In `protocol.py` the
fields are declared once as their C types; the `struct` format is derived from
that declaration, so there is no second copy to forget.

## Conventions

- Comments explain *why*, not what. The existing files are the reference for
  density and tone — match them rather than adding a header block to every
  function.
- Errors that a bench operator can cause (bad pin, out-of-range setpoint,
  unplugged cable) get a message naming the valid range or the likely cause.
  The command layer returns them as `ok=False` rather than raising, because
  the dashboard must survive them.
- Safe states are not optional: `Bench.stop()` drops the pump *before* closing
  valves, and the CLI calls it from a `finally`. Keep that ordering.
- `-std=` and warning flags do not go in `build_flags` — PlatformIO shares
  `CCFLAGS` between gcc and g++, so C-only flags break the C++ build. Warnings
  are scoped in `build_src_flags` and `firmware/lib/ngs/library.json`.

## Where the tuning lives

The board, in NVM (`ngs_store.c`), read back by the host with
GET_CONTROL_CFG. It is the authority: gains only mean anything against a
particular pump, line and flow meter, and the board is bolted to those.

Stored with magic, version and CRC. Flash writes are not atomic and a brownout
mid-write leaves a half-updated block; a config made of two different ones
would look perfectly plausible without the check. Mode and setpoint are
stripped on the way in, so a board can never power up already driving a pump.
Writes are explicit -- it is flash, with a finite erase budget.

Only a *stored* config is adopted by the host. A board that was never saved to
reports the firmware's generic defaults, and BENCH_CONFIG's are specific to
this rig. The calibration is never taken from the device at all.

`store.py` still writes `tuning.json`, but purely as a record: reviewable
history, never read back as configuration. One authority.

`host/tests/conftest.py` points that record at a temp file for every test.
Without it the suite writes into the operator's bench configuration.

## Live plots

`history.py` is a preallocated ring; the browser keeps its own in `plot.js` and
asks `/api/history?since=N` for what is new. Attaching pulls a min/max
decimated overview -- fixed payload whatever is stored, and min/max rather than
an average because averaging erases the excursions worth seeing.

Traces come from BENCH_CONFIG via `traces_for`, so a new channel is plottable
with no edit to the plotting code. Nothing loads from the network.

If you touch this, the properties to preserve are: recording allocates nothing
per sample, a caught-up client transfers nothing, and a decimation pass is
bounded by bucket count rather than buffer size. `test_history.py` pins all
three.

## The emergency stop

Three properties, and each is load-bearing:

- **Atomic on the device.** One message; the board walks its own safe-state
  table. A host-side sequence of writes can half-succeed.
- **Latched.** Output commands return `NGS_ERR_ESTOP` until cleared. Reads and
  "return to manual" stay allowed.
- **Independent of the host.** The watchdog latches when no frame arrives for
  `watchdog_ms`. That is the case a host-side stop cannot cover.

The safe-state table is registered by the host (`Bench.register_safe_state`)
because the firmware does not know what a valve is. It is sent one entry per
message so every payload stays a flat struct of scalars, which is what keeps
the mirror verifiable field by field.

The watchdog defaults to off. Think before changing that: always-on means a
valve set from a one-shot command gets closed a few seconds later, which is
ordinary bench use, not an emergency.

## The control loop

`firmware/lib/ngs/ngs_control.c` runs the closed-loop pump control on the
device at 50 Hz. It lives in firmware because a loop paced by USB round trips
inherits the host's scheduling jitter. It touches no hardware — the caller
reads the ADC and writes the PWM — so it is testable off-target with scripted
measurements, which is the only practical way to test a controller.

`host/ngs_host/control.py` is a Python mirror of it, so the simulator behaves
like the board. Duplicated logic drifts, so the two are pinned together by a
shared numeric vector: the same scenario and the same expected outputs in
`host/tests/test_control_vector.py` and `test_control_vector()` in
`firmware/test/test_ngs/test_control.h`. **The C is authoritative** — when they
disagree, the mirror is wrong. It has already caught one real divergence
(Python's `statistics.median` averages the two middle samples on an even
window; the C takes the upper middle).

The device works in mL/min, not counts, so setpoints and gains mean something.
It gets there from two calibration numbers the host sends; the calibration
itself still lives in `BENCH_CONFIG`.

Things that look like details and are not:

- Entering AUTO seeds the setpoint ramp on the *first tick*, not at configure
  time, because the measurement chain only runs while the loop is active.
- `Bench.stop()` drops the loop to manual before writing the PWM. The device
  refuses manual writes while the loop owns the output, so without that the
  emergency stop fails with BUSY.
- Autotune rejects an experiment whose swing barely clears the hysteresis
  band. Ku divides by `sqrt(a² - h²)`, so a marginal swing does not give an
  uncertain gain, it gives an enormous one.

## Testing without hardware

`fake.py` reimplements the firmware's observable behaviour (same responses,
same error codes for the same bad input); `sim.py` adds a crude flow model on
top so the UI has something to display. Both are for testing the *host*. They
prove the host agrees with what the C is specified to do — they cannot catch a
bug in the C, which is what `pio test -e teensy41` is for.

When you change firmware behaviour, change the fake to match, or the host
tests will keep passing against a device that no longer exists.
