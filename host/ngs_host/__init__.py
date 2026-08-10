"""Python host for the NextGen Stand Teensy 4.1 bench firmware.

Layers, bottom up -- each one only knows about the one below it:

    protocol.py   the wire format, mirrored from firmware/lib/ngs/ngs_protocol.h
    link.py       COBS framing and CRC-16
    device.py     one method per protocol message, over a serial port
    bench.py      what is actually wired up: valves, flow meter, pump
    commands.py   the "V1O;P50;" command language
    ui.py         the 2 Hz dashboard

    fake.py       a simulated device (firmware behaviour, no board)
    sim.py        a simulated plant (flow responds to the pump)

Typical use:

    from ngs_host import Bench, Device

    with Device.open() as dev:
        bench = Bench(dev)
        bench.initialize()
        bench.set_valve("valve1", True)
        bench.set_pwm("pump", 40.0)
        print(bench.read_analog("flow").value)
"""

from .bench import BENCH_CONFIG, AnalogInputSpec, Bench, BenchConfig, PwmOutputSpec, ValveSpec
from .commands import execute, execute_line
from .device import Device, ProtocolMismatch, Timeout, find_ports
from .link import Decoder, FramingError, crc16, encode_frame
from .protocol import ErrCode, MsgType, NgsError, PinMode

__version__ = "0.1.0"

__all__ = [
    "BENCH_CONFIG",
    "AnalogInputSpec",
    "Bench",
    "BenchConfig",
    "Decoder",
    "Device",
    "ErrCode",
    "FramingError",
    "MsgType",
    "NgsError",
    "PinMode",
    "ProtocolMismatch",
    "PwmOutputSpec",
    "Timeout",
    "ValveSpec",
    "crc16",
    "encode_frame",
    "execute",
    "execute_line",
    "find_ports",
]
