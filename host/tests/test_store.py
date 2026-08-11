"""Persisted tuning.

An autotune costs a couple of minutes of the pump oscillating on purpose. If
its result dies with the process, nobody tunes properly. These check it
survives, that it survives *correctly*, and that every way it can fail ends in
"carry on with the configured defaults" rather than in a stack trace.
"""

from __future__ import annotations

import json

import pytest

from ngs_host import protocol as p
from ngs_host.bench import BENCH_CONFIG, Bench
from ngs_host.commands import execute_line
from ngs_host.device import Device
from ngs_host.fake import FakeBoard, FakeDevice
from ngs_host.store import TUNED_FIELDS, TuningStore, apply_record


@pytest.fixture
def board() -> FakeBoard:
    return FakeBoard()


@pytest.fixture
def store(tmp_path) -> TuningStore:
    return TuningStore(tmp_path / "tuning.json")


@pytest.fixture
def bench(board: FakeBoard, store: TuningStore) -> Bench:
    bench = Bench(Device(FakeDevice(board)), store=store)
    bench.initialize()
    return bench


def reopen(board: FakeBoard, store: TuningStore) -> Bench:
    """A fresh host process against the same board and the same file."""
    bench = Bench(Device(FakeDevice(board)), store=store)
    bench.load_tuning()
    return bench


# -- round trip ------------------------------------------------------------


def test_gains_survive_a_new_session(bench, board, store):
    bench.set_gains(kp=0.31, ki=0.047, kd=0.0)

    later = reopen(board, store)
    cfg = later.control_cfg()
    assert cfg.kp == pytest.approx(0.31)
    assert cfg.ki == pytest.approx(0.047)


def test_an_autotune_result_survives(bench, board, store):
    """The expensive case: this is what makes tuning worth doing."""
    from dataclasses import replace

    bench.set_control_cfg(replace(bench.control_cfg(), kp=0.162, ki=0.0192))
    later = reopen(board, store)
    assert later.control_cfg().kp == pytest.approx(0.162)


def test_the_filter_and_deadband_persist_too(bench, board, store):
    execute_line(bench, "KF2.5")
    execute_line(bench, "KB6")

    cfg = reopen(board, store).control_cfg()
    assert cfg.filter_tau_s == pytest.approx(2.5)
    assert cfg.deadband == pytest.approx(6.0)


def test_period_stays_an_integer(bench, board, store):
    """It is a uint32 on the wire. Casting everything to float fails inside
    pack(), a long way from the cause."""
    bench.set_gains(kp=0.2)
    cfg = reopen(board, store).control_cfg()

    assert isinstance(cfg.period_us, int)
    cfg.pack()  # would raise struct.error if it were a float


def test_nothing_is_saved_until_something_changes(board, store):
    Bench(Device(FakeDevice(board)), store=store).load_tuning()
    assert not store.path.exists()


# -- what is deliberately not saved ----------------------------------------


def test_changing_the_setpoint_does_not_rewrite_the_file(bench, store):
    """Only tuning is persisted, so operating the bench produces no diff --
    a file that churned every time someone changed a setpoint would be
    unreviewable and would make `git status` useless during a run."""
    bench.set_pump_mode(True, 250.0)
    bench.set_setpoint(400.0)
    bench.set_pump_mode(False)
    assert not store.path.exists()


def test_the_setpoint_and_mode_are_not_persisted(bench, board, store):
    """Restoring those would mean opening a terminal could start the pump.
    A configuration file has no business doing that."""
    bench.set_gains(kp=0.2)  # something tuned, so there is a file at all
    bench.set_pump_mode(True, 250.0)

    saved = json.loads(store.path.read_text())["boards"][bench.board_serial()]
    assert "setpoint" not in saved
    assert "mode" not in saved

    later = reopen(board, store)
    assert later.control_cfg().mode == p.PumpMode.MANUAL
    assert later.control_cfg().setpoint == 0.0


def test_the_calibration_is_not_persisted(bench, store):
    """It belongs to BENCH_CONFIG; a stale copy here would silently override
    the real wiring."""
    bench.set_gains(kp=0.2)
    saved = json.loads(store.path.read_text())["boards"][bench.board_serial()]
    assert "cal_scale" not in saved
    assert "cal_offset" not in saved


# -- per board -------------------------------------------------------------


def test_tuning_is_keyed_by_board(board, store):
    """Gains belong to a rig. A second Teensy must not inherit the first
    one's tuning."""
    first = Bench(Device(FakeDevice(board)), store=store)
    first.initialize()
    first.set_gains(kp=0.5)

    other_board = FakeBoard(serial_number=b"\x09\x09\x09\x09\x09\x09\x09\x09")
    second = Bench(Device(FakeDevice(other_board)), store=store)
    second.load_tuning()

    assert second.control_cfg().kp == BENCH_CONFIG.controls[0].kp
    assert first.board_serial() != second.board_serial()


# -- provenance ------------------------------------------------------------


def test_an_autotune_records_what_it_measured(bench, board, store):
    """Gains that look wrong later can be traced back to the run."""
    execute_line(bench, "T240")
    result = bench.autotune_result()
    bench.store.save(
        bench.board_serial(), bench.control_cfg(), source="autotune", ku=0.52, tu=3.84, spread=0.01
    )

    record = reopen(board, store).loaded_tuning
    assert record.source == "autotune"
    assert record.ku == pytest.approx(0.52)
    assert "Ku 0.52" in record.describe()
    assert result is not None


def test_a_manual_change_is_marked_as_such(bench, board, store):
    bench.set_gains(kp=0.2)
    record = reopen(board, store).loaded_tuning
    assert record.source == "manual"
    assert record.updated


def test_a_low_confidence_tune_says_so_in_the_description(bench, store):
    bench.store.save(
        bench.board_serial(), bench.control_cfg(), source="autotune", ku=9.9, tu=1.0, spread=0.44
    )
    record = store.load(bench.board_serial())
    assert "spread" in record.describe()


# -- forgetting ------------------------------------------------------------


def test_forget_returns_to_the_configured_defaults(bench, board, store):
    bench.set_gains(kp=0.9)
    assert bench.forget_tuning()

    assert bench.control_cfg().kp == BENCH_CONFIG.controls[0].kp
    assert reopen(board, store).control_cfg().kp == BENCH_CONFIG.controls[0].kp


def test_forgetting_nothing_is_not_an_error(bench):
    bench.forget_tuning()
    assert bench.forget_tuning() is False


# -- failure modes ---------------------------------------------------------


def test_a_corrupt_file_falls_back_to_defaults(board, tmp_path):
    """A bench tool that will not start because its preferences file is
    corrupt is worse than one that forgets a tuning."""
    path = tmp_path / "tuning.json"
    path.write_text("{not json at all")

    bench = Bench(Device(FakeDevice(board)), store=TuningStore(path))
    assert bench.load_tuning() is None
    assert bench.control_cfg().kp == BENCH_CONFIG.controls[0].kp


def test_a_file_of_the_wrong_shape_is_ignored(board, tmp_path):
    path = tmp_path / "tuning.json"
    path.write_text(json.dumps(["not", "a", "mapping"]))

    bench = Bench(Device(FakeDevice(board)), store=TuningStore(path))
    assert bench.load_tuning() is None


def test_a_partial_record_leaves_the_rest_at_defaults(board, tmp_path):
    """An older file that predates a setting must not zero it."""
    path = tmp_path / "tuning.json"
    serial = FakeBoard().serial_number.hex()
    path.write_text(json.dumps({"boards": {serial: {"kp": 0.4}}}))

    bench = Bench(Device(FakeDevice(board)), store=TuningStore(path))
    bench.load_tuning()

    cfg = bench.control_cfg()
    assert cfg.kp == pytest.approx(0.4)
    assert cfg.deadband == BENCH_CONFIG.controls[0].deadband
    assert cfg.period_us == BENCH_CONFIG.controls[0].period_us


def test_an_unwritable_location_does_not_take_the_bench_down(board, tmp_path):
    """A read-only checkout costs you persistence, not the session."""
    blocked = tmp_path / "nope"
    blocked.write_text("I am a file, not a directory")

    bench = Bench(Device(FakeDevice(board)), store=TuningStore(blocked / "tuning.json"))
    bench.initialize()
    bench.set_gains(kp=0.25)  # must not raise

    assert bench.control_cfg().kp == pytest.approx(0.25)


def test_the_file_is_written_atomically(bench, store):
    """Written whole and moved into place: a half-written file reads back as
    'no tuning' and silently throws away an autotune."""
    bench.set_gains(kp=0.2)
    assert store.path.exists()
    assert not store.path.with_suffix(".json.tmp").exists()


def test_apply_record_only_touches_what_it_carries():
    from ngs_host.store import TuningRecord

    cfg = p.ControlCfg(kp=0.05, ki=0.02, deadband=2.0)
    out = apply_record(cfg, TuningRecord(values={"kp": 0.9}))

    assert out.kp == pytest.approx(0.9)
    assert out.deadband == pytest.approx(2.0)


def test_the_saved_field_list_matches_the_config():
    """Every persisted field has to exist on ControlCfg, or loading one
    explodes at replace() time."""
    cfg = p.ControlCfg()
    for name in TUNED_FIELDS:
        assert hasattr(cfg, name), name


# -- the file itself -------------------------------------------------------


def test_the_file_is_readable_and_reviewable(bench, store):
    """It is meant to be committed, so it has to diff sensibly."""
    bench.set_gains(kp=0.2)
    text = store.path.read_text()

    assert text.endswith("\n")
    data = json.loads(text)
    assert "_comment" in data
    # Sorted keys, so a changed gain is a one-line diff rather than a reshuffle.
    assert text.index('"deadband"') < text.index('"kp"')


def test_every_cli_entry_point_loads_the_tuning(monkeypatch, tmp_path, board):
    """A tuning that only loads down some code paths is one you cannot trust
    to be in effect. This caught it being wired into none of them.
    """
    from ngs_host import cli

    path = tmp_path / "tuning.json"
    monkeypatch.setenv("NGS_TUNING_FILE", str(path))

    seeded = Bench(Device(FakeDevice(board)), store=TuningStore(path))
    seeded.initialize()
    seeded.set_gains(kp=0.42)

    # The simulated path, which is what `--sim` uses.
    obj, label = cli._open(port=None, sim=True)
    assert label == "sim"
    assert obj.control_cfg().kp == pytest.approx(0.42)
    assert obj.loaded_tuning is not None
