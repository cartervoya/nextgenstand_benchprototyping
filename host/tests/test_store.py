"""Tuning, with the board as the authority.

The device holds its own gains in NVM. Gains only mean anything against a
particular pump, line and flow meter, and the board is the thing bolted to
those -- so a fresh checkout, a different laptop, or no host config at all
still drives the rig the way it was actually set up.

`tuning.json` is still written, but only as a record: reviewable history, never
read back as configuration. One authority, or you eventually run gains you did
not choose.

The NVM itself -- CRC, versioning, what a brownout mid-write leaves behind --
is tested on the board in firmware/test/test_ngs/test_store.h.
"""

from __future__ import annotations

import json

import pytest

from ngs_host import protocol as p
from ngs_host.bench import BENCH_CONFIG, Bench
from ngs_host.commands import execute_line
from ngs_host.device import Device
from ngs_host.fake import FakeBoard, FakeDevice
from ngs_host.store import TUNED_FIELDS, TuningStore


@pytest.fixture
def board() -> FakeBoard:
    return FakeBoard()


@pytest.fixture
def store(tmp_path) -> TuningStore:
    return TuningStore(tmp_path / "tuning.json")


@pytest.fixture
def fake(board: FakeBoard) -> FakeDevice:
    return FakeDevice(board)


@pytest.fixture
def bench(fake: FakeDevice, store: TuningStore) -> Bench:
    bench = Bench(Device(fake), store=store)
    bench.initialize()
    return bench


def power_cycle(fake: FakeDevice, store: TuningStore) -> Bench:
    """A board that lost power and a host that started fresh.

    Everything in RAM goes; the NVM does not -- which is the whole point of
    putting the tuning there.
    """
    rebooted = FakeDevice(FakeBoard(), nvm=fake.nvm)
    bench = Bench(Device(rebooted), store=store)
    bench.load_tuning()
    return bench


# -- the board is the authority --------------------------------------------


def test_gains_survive_a_power_cycle(bench, fake, store):
    bench.set_gains(kp=0.31, ki=0.047)

    later = power_cycle(fake, store)
    cfg = later.control_cfg()
    assert cfg.kp == pytest.approx(0.31)
    assert cfg.ki == pytest.approx(0.047)


def test_the_deadzone_survives_too(bench, fake, store):
    from dataclasses import replace

    bench.set_control_cfg(replace(bench.control_cfg(), out_deadzone=21.5))
    assert power_cycle(fake, store).control_cfg().out_deadzone == pytest.approx(21.5)


def test_a_host_with_no_record_still_gets_the_board_tuning(bench, fake, tmp_path):
    """The case that motivates board-side storage: a different laptop."""
    bench.set_gains(kp=0.44)

    elsewhere = TuningStore(tmp_path / "somewhere-else.json")
    assert not elsewhere.path.exists()

    later = power_cycle(fake, elsewhere)
    assert later.control_cfg().kp == pytest.approx(0.44)


def test_the_host_file_is_not_read_as_configuration(bench, fake, store):
    """One authority. A file left over from another rig must not quietly
    become the gains in effect."""
    bench.set_gains(kp=0.2)

    store.save(
        bench.board_serial(),
        p.ControlCfg(kp=9.9, ki=9.9),
        source="hand-edited nonsense",
    )

    later = power_cycle(fake, store)
    assert later.control_cfg().kp == pytest.approx(0.2)


def test_loading_says_where_the_tuning_came_from(bench, fake, store):
    bench.set_gains(kp=0.2)
    assert power_cycle(fake, store).loaded_tuning.source == "board"


def test_a_board_with_nothing_stored_reports_defaults(board, store):
    fresh = Bench(Device(FakeDevice(board)), store=store)
    record = fresh.load_tuning()
    assert record.source == "firmware defaults"


# -- what the board is not asked for ---------------------------------------


def test_the_calibration_is_never_taken_from_the_device(bench, fake, store):
    """It describes the wiring, it lives in BENCH_CONFIG, and a stale stored
    copy would silently rescale every reading."""
    fake.nvm = p.ControlCfg(kp=0.3, cal_scale=999.0, cal_offset=999.0)

    later = power_cycle(fake, store)
    cfg = later.control_cfg()
    assert cfg.kp == pytest.approx(0.3)
    assert cfg.cal_scale != pytest.approx(999.0)
    assert cfg.cal_offset != pytest.approx(999.0)
    assert "cal_scale" not in TUNED_FIELDS


def test_a_saved_board_never_comes_up_running(bench, fake, store):
    """Mode and setpoint are stripped on the way into NVM: powering up already
    driving a pump, because that is what it was doing, is not a thing this
    bench does."""
    bench.set_pump_mode(True, 250.0)
    bench.save_to_board()

    assert fake.nvm.mode == p.PumpMode.MANUAL
    assert fake.nvm.setpoint == 0.0

    later = power_cycle(fake, store)
    assert later.control_state().mode == p.PumpMode.MANUAL


def test_saving_does_not_start_the_pump(bench, fake):
    """The save itself sends MANUAL, so it can never be the thing that starts
    a pump -- and it must put the mode back afterwards if it was running."""
    bench.set_pump_mode(True, 200.0)
    bench.save_to_board()
    assert bench.control_state().mode == p.PumpMode.AUTO


# -- the record ------------------------------------------------------------


def test_the_file_still_records_what_was_saved(bench, store):
    bench.set_gains(kp=0.162, ki=0.0192)

    saved = json.loads(store.path.read_text())["boards"][bench.board_serial()]
    assert saved["kp"] == pytest.approx(0.162)
    assert saved["source"] == "manual"
    assert saved["updated"]


def test_an_autotune_records_what_it_measured(bench, store):
    bench.store.save(
        bench.board_serial(), bench.control_cfg(), source="autotune", ku=0.52, tu=3.84, spread=0.01
    )
    record = store.load(bench.board_serial())
    assert record.source == "autotune"
    assert "Ku 0.52" in record.describe()


def test_changing_the_setpoint_writes_nothing(bench, store):
    """Operating the bench must not churn the record, or `git status` is
    useless during a run -- and it must not spend flash endurance either."""
    bench.set_pump_mode(True, 250.0)
    bench.set_setpoint(400.0)
    assert not store.path.exists()


def test_an_unwritable_record_does_not_take_the_bench_down(bench, tmp_path, fake):
    """A read-only checkout costs you the history, not the session -- the
    tuning itself is on the board either way."""
    blocked = tmp_path / "nope"
    blocked.write_text("I am a file, not a directory")
    bench.store = TuningStore(blocked / "tuning.json")

    bench.set_gains(kp=0.25)  # must not raise

    assert bench.control_cfg().kp == pytest.approx(0.25)
    assert fake.nvm.kp == pytest.approx(0.25)


# -- forgetting ------------------------------------------------------------


def test_forget_erases_the_board_too(bench, fake, store):
    bench.set_gains(kp=0.9)
    assert fake.nvm is not None

    bench.forget_tuning()

    assert fake.nvm is None
    assert bench.control_cfg().kp == BENCH_CONFIG.controls[0].kp
    assert power_cycle(fake, store).control_cfg().kp == BENCH_CONFIG.controls[0].kp


# -- entry points ----------------------------------------------------------


def test_every_cli_entry_point_loads_from_the_board(monkeypatch, tmp_path, board):
    """A tuning that only loads down some code paths is one you cannot trust
    to be in effect. This caught it being wired into none of them."""
    from ngs_host import cli

    monkeypatch.setenv("NGS_TUNING_FILE", str(tmp_path / "tuning.json"))

    seeded = Bench(Device(FakeDevice(board)), store=TuningStore(tmp_path / "t.json"))
    seeded.initialize()
    seeded.set_gains(kp=0.42)
    stored = seeded.device._t.nvm

    monkeypatch.setattr(cli, "make_sim_device", lambda *a, **k: FakeDevice(FakeBoard(), nvm=stored))
    obj, label = cli._open(port=None, sim=True)

    assert label == "sim"
    assert obj.control_cfg().kp == pytest.approx(0.42)


def test_commands_report_the_gains_the_board_holds(bench, fake, store):
    bench.set_gains(kp=0.33)
    later = power_cycle(fake, store)

    (result,) = execute_line(later, "K?")
    assert "0.33" in result.text
    assert "board" in result.text


def test_the_saved_field_list_matches_the_config():
    cfg = p.ControlCfg()
    for name in TUNED_FIELDS:
        assert hasattr(cfg, name), name
