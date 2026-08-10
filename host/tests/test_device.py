"""Driver tests, end to end against the simulated firmware in fake.py."""

from __future__ import annotations

import pytest

from ngs_host import protocol as p
from ngs_host.device import Device, ProtocolMismatch, Timeout
from ngs_host.fake import ADC_BITS, FakeBoard, FakeDevice
from ngs_host.link import encode_frame


@pytest.fixture
def board() -> FakeBoard:
    return FakeBoard()


@pytest.fixture
def dev(board: FakeBoard) -> Device:
    return Device(FakeDevice(board), timeout=1.0)


# -- basics ----------------------------------------------------------------


def test_ping_returns_the_device_clock(dev):
    assert dev.ping().uptime_us >= 0


def test_info(dev, board):
    info = dev.info()
    assert info.proto_version == p.PROTO_VERSION
    assert info.fw_version == "0.1.0"
    assert info.cpu_hz == board.cpu_hz
    assert info.mcu_serial == board.serial_number
    assert info.max_payload == p.MAX_PAYLOAD


def test_info_rejects_a_mismatched_protocol_version(board):
    fake = FakeDevice(board)
    dev = Device(fake)

    original = fake._get_info

    def stale_info(frame):
        original(frame)
        # Rewrite the queued response as if an old firmware answered.
        fake._tx.clear()
        info = p.Info(99, 0, 0, 9, board.cpu_hz, p.MAX_PAYLOAD, board.serial_number)
        fake._send(frame.type | p.MSG_RESP, frame.seq, info.pack())
        return None

    fake._get_info = stale_info
    with pytest.raises(ProtocolMismatch, match="v99"):
        dev.info()


def test_status_counts_frames(dev):
    dev.ping()
    dev.ping()
    status = dev.status()
    assert status.rx_frames >= 3  # two pings plus this request
    assert status.tx_frames >= 3
    assert status.rx_crc_errors == 0


def test_loop_max_is_read_and_clear(dev):
    dev.status()
    assert dev.status().loop_max_us == 0


def test_startup_log_is_captured(dev):
    dev.ping()  # any traffic pumps the receive path
    assert "ngs firmware ready" in dev.logs


def test_log_callback(board):
    seen: list[str] = []
    dev = Device(FakeDevice(board), on_log=seen.append)
    dev.ping()
    assert seen == ["ngs firmware ready"]


# -- gpio ------------------------------------------------------------------


def test_gpio_write_then_read_back(dev, board):
    dev.set_gpio(32, True)
    assert board.pin_values[32] == 1
    assert dev.get_gpio(32, p.PinMode.OUTPUT) is True

    dev.set_gpio(32, False)
    assert dev.get_gpio(32, p.PinMode.OUTPUT) is False


def test_input_pullup_reads_high_by_default(dev):
    assert dev.get_gpio(5, p.PinMode.INPUT_PULLUP) is True


def test_bad_pin_is_caught_on_the_host_without_a_round_trip(dev):
    with pytest.raises(ValueError, match="outside"):
        dev.set_gpio(200, True)


def test_bad_pin_from_the_wire_is_refused_by_the_device(dev):
    """Bypasses the host-side check to prove the firmware validates too."""
    with pytest.raises(p.NgsError) as exc:
        dev.transact(p.MsgType.SET_GPIO, p.GpioSet(pin=99, value=1, mode=0).pack())
    assert exc.value.code is p.ErrCode.BAD_ARGUMENT


# -- adc -------------------------------------------------------------------


def test_read_adc(dev, board):
    board.adc = lambda channel: 1000 + channel
    reading = dev.read_adc(13)
    assert reading.channel == 13
    assert reading.raw == 1013
    assert reading.resolution == ADC_BITS
    assert reading.normalized == pytest.approx(1013 / 4095)


def test_adc_averaging_happens_on_the_device(dev, board):
    values = iter([100, 200, 300, 400])
    board.adc = lambda channel: next(values)
    assert dev.read_adc(0, samples=4).raw == 250


def test_adc_channel_is_range_checked(dev):
    with pytest.raises(ValueError, match="A17"):
        dev.read_adc(18)


def test_out_of_range_channel_from_the_wire_is_refused(dev):
    with pytest.raises(p.NgsError) as exc:
        dev.transact(p.MsgType.READ_ADC, p.AdcRead(channel=40, samples=1).pack())
    assert exc.value.code is p.ErrCode.BAD_ARGUMENT


# -- pwm -------------------------------------------------------------------


def test_write_pwm_records_duty_frequency_and_resolution(dev, board):
    dev.write_pwm(33, duty=2048, freq_hz=50_000, resolution=12)
    assert board.pwm[33] == (2048, 50_000, 12)


def test_duty_beyond_the_resolution_is_refused(dev):
    with pytest.raises(p.NgsError) as exc:
        dev.write_pwm(33, duty=4096, resolution=12)
    assert exc.value.code is p.ErrCode.BAD_ARGUMENT


# -- error handling --------------------------------------------------------


def test_unknown_message_type(dev):
    with pytest.raises(p.NgsError) as exc:
        dev.transact(0x7E, b"")  # type: ignore[arg-type]
    assert exc.value.code is p.ErrCode.UNKNOWN_TYPE


def test_wrong_payload_size(dev):
    with pytest.raises(p.NgsError) as exc:
        dev.transact(p.MsgType.SET_GPIO, b"\x01")
    assert exc.value.code is p.ErrCode.BAD_PAYLOAD


def test_error_identifies_the_request_that_failed(dev):
    with pytest.raises(p.NgsError) as exc:
        dev.transact(p.MsgType.SET_GPIO, b"\x01")
    assert exc.value.msg_type == p.MsgType.SET_GPIO
    assert exc.value.seq != 0


def test_corrupt_inbound_frame_does_not_wedge_the_link(dev):
    dev._t.write(b"\x05\x01\x02\x03\x04\x00")  # garbage, then a real command
    assert dev.ping().uptime_us >= 0


class SilentTransport:
    """Accepts everything, answers nothing."""

    def read(self, size: int = 1) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        pass


def test_timeout_names_the_command():
    dev = Device(SilentTransport(), timeout=0.05)
    with pytest.raises(Timeout, match="PING"):
        dev.ping()


def test_a_stale_response_does_not_satisfy_the_next_request(board):
    """A response to a request that already timed out must be discarded, not
    handed to whatever is waiting now."""
    fake = FakeDevice(board)
    dev = Device(fake, timeout=0.2)
    fake._tx += encode_frame(p.MsgType.PING | p.MSG_RESP, 200, p.Pong(1).pack())

    pong = dev.ping()  # answered by the real reply, not the stale one
    assert pong.uptime_us != 1


# -- telemetry -------------------------------------------------------------


def test_stream_yields_records_and_stops_cleanly(dev, board):
    board.adc = lambda channel: 500 + channel
    records = list(dev.stream(channels=[0, 2], period_us=1_000, count=3))

    assert len(records) == 3
    assert [r.seq for r in records] == [0, 1, 2]
    assert records[0].channels == {0: 500, 2: 502}
    assert records[0].resolution == ADC_BITS
    assert not dev._t.stream_enabled, "streaming must be off once the caller is done"


def test_stream_stops_even_if_the_caller_breaks_out(dev):
    for _ in dev.stream(channels=[0], period_us=1_000):
        break
    assert not dev._t.stream_enabled


def test_streaming_needs_a_period_and_a_channel(dev):
    with pytest.raises(ValueError, match="period"):
        dev.set_stream(True, period_us=0, channels=[0])
    with pytest.raises(ValueError, match="channel"):
        dev.set_stream(True, period_us=1000, channels=[])


def test_device_rejects_an_impossible_channel_mask(dev):
    with pytest.raises(p.NgsError) as exc:
        dev.transact(
            p.MsgType.SET_STREAM,
            p.StreamCfg(enable=1, period_us=1000, channel_mask=1 << 20).pack(),
        )
    assert exc.value.code is p.ErrCode.BAD_ARGUMENT


def test_telemetry_arriving_mid_command_is_not_mistaken_for_a_response(dev, board):
    dev.set_stream(True, period_us=100, channels=[0])
    try:
        # Plenty of telemetry is queued ahead of this response.
        assert dev.ping().uptime_us >= 0
        assert dev.status().rx_frames > 0
    finally:
        dev.set_stream(False)


# -- lifecycle -------------------------------------------------------------


def test_context_manager_closes_the_transport(board):
    fake = FakeDevice(board)
    with Device(fake) as dev:
        dev.ping()
    assert fake.closed


def test_sequence_numbers_never_reuse_zero(dev):
    seqs = {dev._next_seq() for _ in range(600)}
    assert 0 not in seqs
    assert seqs == set(range(1, 256))
