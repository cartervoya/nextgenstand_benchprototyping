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

## Testing without hardware

`fake.py` reimplements the firmware's observable behaviour (same responses,
same error codes for the same bad input); `sim.py` adds a crude flow model on
top so the UI has something to display. Both are for testing the *host*. They
prove the host agrees with what the C is specified to do — they cannot catch a
bug in the C, which is what `pio test -e teensy41` is for.

When you change firmware behaviour, change the fake to match, or the host
tests will keep passing against a device that no longer exists.
