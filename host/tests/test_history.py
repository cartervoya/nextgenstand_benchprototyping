"""The recorded trace behind the live plots.

What matters here is that it stays cheap as it fills, that a client only ever
transfers what is new, and that decimation preserves spikes -- which is the
whole reason for watching a noisy flow signal.
"""

from __future__ import annotations

import math

import pytest

from ngs_host.bench import BENCH_CONFIG
from ngs_host.history import History, TraceSpec, sample_from, traces_for

TRACES = (TraceSpec("flow", "Flow", "mL/min"), TraceSpec("out", "Output", "%", axis="right"))


@pytest.fixture
def history() -> History:
    return History(traces=TRACES, capacity=100)


def fill(history: History, n: int, start: float = 0.0) -> None:
    for i in range(n):
        history.record(start + i * 0.5, {"flow": float(i), "out": float(i) / 2})


# -- recording -------------------------------------------------------------


def test_records_and_reads_back(history):
    fill(history, 5)
    data = history.since(0)

    assert data["seq"] == 5
    assert data["series"]["flow"] == [0, 1, 2, 3, 4]
    assert data["t"][0] == 0.0


def test_the_ring_wraps_and_keeps_the_newest(history):
    fill(history, 250)  # capacity is 100

    assert history.count == 250
    assert history.oldest == 151

    data = history.since(0)
    assert len(data["series"]["flow"]) == 100
    assert data["series"]["flow"][-1] == 249
    assert data["series"]["flow"][0] == 150


def test_memory_does_not_grow_with_time(history):
    """The buffer is preallocated: recording forever must not allocate."""
    fill(history, 10)
    before = len(history._time), len(history._values["flow"])
    fill(history, 5_000)
    assert (len(history._time), len(history._values["flow"])) == before


# -- incremental reads -----------------------------------------------------


def test_a_client_only_gets_what_is_new(history):
    fill(history, 10)
    first = history.since(0)
    assert len(first["t"]) == 10

    fill(history, 3, start=100.0)
    update = history.since(first["seq"])

    assert len(update["t"]) == 3, "a caught-up client must not re-download the history"
    assert update["seq"] == 13


def test_asking_for_what_you_already_have_returns_nothing(history):
    fill(history, 10)
    assert history.since(10)["t"] == []


def test_a_client_that_fell_behind_the_ring_gets_what_survives(history):
    """Not an error and not a crash: it gets the oldest still held, and its
    cursor moves on."""
    fill(history, 10)
    fill(history, 500, start=100.0)

    update = history.since(5)
    assert len(update["t"]) == 100
    assert update["seq"] == 510


def test_a_missing_value_records_as_a_gap(history):
    """A dropped poll must leave a hole, not a straight line pretending the
    signal held steady."""
    history.record(0.0, {"flow": 1.0, "out": 5.0})
    history.record(0.5, {"flow": None, "out": 5.0})
    history.record(1.0, {"flow": 3.0, "out": 5.0})

    assert history.since(0)["series"]["flow"] == [1.0, None, 3.0]


def test_only_the_requested_traces_are_sent(history):
    fill(history, 5)
    data = history.since(0, keys=["flow"])
    assert set(data["series"]) == {"flow"}


# -- decimation ------------------------------------------------------------


def test_decimation_returns_a_fixed_size_payload(history):
    fill(history, 100)
    for buckets in (10, 50, 100):
        data = history.decimate(buckets=buckets)
        assert len(data["t"]) == buckets
        assert len(data["series"]["flow"]) == buckets


def test_decimation_preserves_spikes(history):
    """Averaging would erase them, and on a noisy signal they are the point."""
    for i in range(100):
        history.record(i * 0.1, {"flow": 1000.0 if i == 47 else 10.0, "out": 0.0})

    data = history.decimate(buckets=10)
    highs = [pair[1] for pair in data["series"]["flow"] if pair[1] is not None]
    assert max(highs) == 1000.0, "the spike was smoothed away"

    lows = [pair[0] for pair in data["series"]["flow"] if pair[0] is not None]
    assert min(lows) == 10.0


def test_decimation_never_asks_for_more_buckets_than_samples(history):
    fill(history, 7)
    data = history.decimate(buckets=1000)
    assert len(data["t"]) == 7


def test_decimation_of_an_empty_buffer_is_empty(history):
    data = history.decimate()
    assert data["t"] == []
    assert data["seq"] == 0


def test_decimation_marks_itself(history):
    """The client draws min/max bands differently from point samples, so it has to
    know which it got."""
    fill(history, 10)
    assert history.decimate()["decimated"] is True
    assert "decimated" not in history.since(0)


def test_gaps_survive_decimation(history):
    for i in range(20):
        history.record(i * 0.1, {"flow": None if 5 <= i < 15 else 1.0, "out": 0.0})

    data = history.decimate(buckets=4)
    assert any(pair == [None, None] for pair in data["series"]["flow"])


# -- traces from the bench config ------------------------------------------


def test_traces_are_derived_from_the_config():
    """A channel added to BENCH_CONFIG becomes plottable with no edit here."""
    specs = traces_for(BENCH_CONFIG)
    keys = {s.key for s in specs}

    assert "flow" in keys
    assert "pump_output" in keys
    assert "pump_setpoint" in keys
    assert "valve1" in keys and "valve2" in keys


def test_flow_and_percent_are_on_different_axes():
    """Otherwise 600 mL/min and 40 % share a scale and both are unreadable."""
    specs = {s.key: s for s in traces_for(BENCH_CONFIG)}
    assert specs["flow"].axis != specs["pump_output"].axis


def test_a_snapshot_flattens_into_a_sample():
    from ngs_host.bench import Bench
    from ngs_host.device import Device
    from ngs_host.fake import FakeDevice

    bench = Bench(Device(FakeDevice()))
    bench.initialize()
    bench.set_valve("valve1", True)

    values = sample_from(bench.poll(), BENCH_CONFIG)

    assert values["valve1"] == 1.0
    assert values["valve2"] == 0.0
    assert values["pump_output"] == 0.0
    assert isinstance(values["flow"], float)


def test_a_faulted_sensor_records_as_a_gap():
    """Plotting a fault as a number would draw a flow that was never measured."""
    from ngs_host.bench import Bench
    from ngs_host.device import Device
    from ngs_host.fake import FakeBoard, FakeDevice

    board = FakeBoard()
    board.adc = lambda channel: 0  # far below 4 mA
    bench = Bench(Device(FakeDevice(board)))
    bench.initialize()

    values = sample_from(bench.poll(), BENCH_CONFIG)
    assert values["flow"] is None


# -- scale -----------------------------------------------------------------


def test_a_large_buffer_stays_responsive():
    """The properties that matter at size: recording is O(1), an incremental
    read moves only what is new, and a decimation pass is bounded."""
    import time

    history = History(traces=TRACES, capacity=50_000)

    start = time.perf_counter()
    for i in range(50_000):
        history.record(i * 0.02, {"flow": float(i % 600), "out": 50.0})
    record_s = time.perf_counter() - start

    start = time.perf_counter()
    update = history.since(history.count - 8)
    incremental_s = time.perf_counter() - start

    start = time.perf_counter()
    data = history.decimate(buckets=1200)
    decimate_s = time.perf_counter() - start

    assert len(update["t"]) == 8
    assert len(data["t"]) == 1200
    # Generous bounds -- this is a smoke test against accidental O(n) per
    # sample or a full copy per poll, not a benchmark.
    assert record_s < 5.0, f"recording 50k samples took {record_s:.2f}s"
    assert incremental_s < 0.01, f"an 8-sample update took {incremental_s * 1000:.1f}ms"
    assert decimate_s < 1.0, f"decimating 50k points took {decimate_s:.2f}s"


def test_span_reports_the_window_held(history):
    fill(history, 10)
    first, last = history.span()
    assert first == 0.0
    assert last == pytest.approx(4.5)


def test_clear_resets_everything(history):
    fill(history, 10)
    history.clear()

    assert history.count == 0
    assert history.since(0)["t"] == []
    assert all(math.isnan(v) for v in history._values["flow"])
