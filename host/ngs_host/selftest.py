"""Hardware self-test: prove the board works before it is wired to anything.

Every check here is safe with nothing connected to the Teensy, and only
meaningful *because* nothing is connected -- it drives the valve pins and the
pump pin to see whether the silicon does what the firmware claims. Run it
again after wiring and those same writes move real hardware.

What this cannot tell you, and no amount of software can:

  - whether the flow scaling is right (needs a real 4-20 mA loop)
  - whether the PWM carrier is really 50 kHz (needs a scope)
  - whether HIGH opens your valves (needs the valves)

Those are called out in the results rather than quietly passed over.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from statistics import mean

from . import protocol as p
from .bench import Bench
from .device import Timeout


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    #: True when the check passed but cannot be fully trusted without hardware
    #: that is not attached yet.
    caveat: bool = False


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, caveat: bool = False) -> Check:
        check = Check(name, ok, detail, caveat)
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def run(bench: Bench, *, pings: int = 500, telemetry_hz: int = 200) -> Report:
    """Run every check, leaving the bench in its safe state."""
    report = Report()
    device = bench.device

    try:
        _identity(device, report)
        _link_soak(device, report, pings)
        _gpio(bench, report)
        _pwm(bench, report)
        _analog(bench, report)
        _telemetry(device, report, telemetry_hz)
        _counters(device, report)
    finally:
        # However this ends, the board does not stay driven.
        try:
            bench.stop()
        except (p.NgsError, Timeout, OSError):
            report.add("safe state", False, "could not return the bench to its safe state")

    return report


def _identity(device, report: Report) -> None:
    info = device.info()
    report.add(
        "identity",
        info.proto_version == p.PROTO_VERSION,
        f"firmware {info.fw_version}, protocol v{info.proto_version}, "
        f"mcu {info.serial_hex}, {info.cpu_hz / 1e6:.0f} MHz",
    )


def _link_soak(device, report: Report, pings: int) -> None:
    """Hammer the link. A marginal cable or a framing bug shows up here as a
    timeout or a CRC error long before it shows up as a bad reading."""
    before = device.status()
    rtts, failures = [], 0

    for _ in range(pings):
        t0 = time.perf_counter()
        try:
            device.ping()
            rtts.append((time.perf_counter() - t0) * 1000)
        except (Timeout, p.NgsError):
            failures += 1

    after = device.status()
    crc = after.rx_crc_errors - before.rx_crc_errors
    overflow = after.rx_overflows - before.rx_overflows
    host_rejects = len(device.framing_errors)

    report.add(
        f"link soak ({pings} round trips)",
        failures == 0 and crc == 0 and overflow == 0 and host_rejects == 0,
        f"{failures} failed, device: {crc} crc / {overflow} overflow, "
        f"host rejected {host_rejects}; "
        f"rtt min {min(rtts):.2f} / mean {mean(rtts):.2f} / max {max(rtts):.2f} ms"
        if rtts
        else "no successful round trips",
    )


def _gpio(bench: Bench, report: Report) -> None:
    """Drive each valve pin and read it back.

    This is the check that proves the readback path works on real silicon --
    the thing the whole output-mismatch warning depends on. With nothing
    connected it is purely a pin test.
    """
    for spec in bench.config.valves:
        results = []
        for want_open in (True, False, True, False):
            bench.set_valve(spec.name, want_open)
            reading = bench.read_valve(spec.name)
            results.append(reading.is_open == want_open)

        ok = all(results)
        bench.set_valve(spec.name, False)
        report.add(
            f"{spec.code} pin {spec.pin} drive/readback",
            ok,
            "follows every command"
            if ok
            else "pin does not read back what was written -- readback unreliable, "
            "so the output-mismatch warning cannot be trusted",
        )


def _pwm(bench: Bench, report: Report) -> None:
    """Sweep the duty cycle across its range, including both endpoints.

    100 % is the interesting one: full scale is 4095 at 12-bit, and 4096 is
    rejected by the firmware, so an off-by-one here fails loudly.
    """
    for spec in bench.config.pwms:
        failures = []
        for percent in (0.0, 1.0, 25.0, 50.0, 99.0, 100.0, 0.0):
            try:
                bench.set_pwm(spec.name, percent)
            except (p.NgsError, ValueError) as exc:
                failures.append(f"{percent:g}%: {exc}")

        bench.set_pwm(spec.name, spec.default_percent)
        report.add(
            f"{spec.code} pin {spec.pin} PWM sweep",
            not failures,
            "0-100 % accepted, full scale "
            f"{spec.to_counts(100.0)}/{spec.max_counts} counts"
            if not failures
            else "; ".join(failures),
        )
        report.add(
            f"{spec.code} carrier frequency",
            True,
            f"requested {spec.freq_hz / 1000:g} kHz -- not verifiable without a scope; "
            "check it before trusting the RC-filtered output",
            caveat=True,
        )


def _analog(bench: Bench, report: Report) -> None:
    """Sample each analog channel and describe what it is doing.

    With nothing connected the value is meaningless, but the *behaviour* is
    not: a floating input wanders, while a driven one sits still. Reporting
    the spread makes the difference visible rather than leaving a plausible
    number on screen.
    """
    for spec in bench.config.analogs:
        samples = []
        for _ in range(20):
            reading = bench.read_analog(spec.name)
            samples.append(reading.volts)
            time.sleep(0.01)

        spread = max(samples) - min(samples)
        in_range = all(0.0 <= v <= 3.4 for v in samples)
        looks_floating = spread > 0.02 or not (
            spec.v_min - spec.fault_margin_v <= mean(samples) <= spec.v_max
        )

        report.add(
            f"{spec.code} pin {spec.pin} ADC",
            in_range,
            f"mean {mean(samples):.3f} V, spread {spread * 1000:.0f} mV"
            + (
                "  -- wandering, consistent with an unconnected input"
                if looks_floating
                else "  -- steady"
            ),
            caveat=looks_floating,
        )


def _telemetry(device, report: Report, hz: int) -> None:
    """Stream hard and look for gaps.

    Telemetry carries its own sequence counter precisely so dropped records
    are detectable. The firmware drops a record rather than block when the USB
    buffer is full, so this measures whether the host keeps up.
    """
    period_us = int(1_000_000 / hz)
    want = 200
    records = list(device.stream(channels=[0], period_us=period_us, count=want, duration=10.0))

    if not records:
        report.add(f"telemetry @ {hz} Hz", False, "no records received")
        return

    seqs = [r.seq for r in records]
    gaps = sum(b - a - 1 for a, b in zip(seqs, seqs[1:], strict=False) if b > a + 1)
    lost_pct = gaps / (seqs[-1] - seqs[0] + 1) * 100 if seqs[-1] > seqs[0] else 0.0

    report.add(
        f"telemetry @ {hz} Hz",
        lost_pct <= 5.0,
        f"{len(records)} records, {gaps} dropped ({lost_pct:.1f} %)",
    )


def _counters(device, report: Report) -> None:
    status = device.status()
    report.add(
        "final counters",
        status.rx_crc_errors == 0 and status.rx_overflows == 0,
        f"rx {status.rx_frames}, tx {status.tx_frames}, "
        f"crc {status.rx_crc_errors}, overflow {status.rx_overflows}, "
        f"loop max {status.loop_max_us} us, {status.temp_c:.1f} C",
    )
