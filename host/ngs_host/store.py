"""Persisted tuning: the gains survive closing the terminal.

An autotune takes a couple of minutes of the pump oscillating on purpose. Its
result living only in the process that ran it means doing that again every
session, which is both tedious and a reason not to bother tuning properly.

Where it lives, and why here rather than on the board:

  - Tuning is bench configuration, and in this project bench configuration
    lives on the host, under version control, next to the calibration it
    depends on. Gains are only meaningful against a particular pump, line and
    flow meter -- exactly the things BENCH_CONFIG describes.
  - A JSON file diffs. "kp went from 0.16 to 0.31 on the 12th" is a question
    you can answer from git; EEPROM cannot be reviewed, and a board swap takes
    its contents with it.
  - The device is handed its configuration before the loop is ever enabled, so
    it has no use for a copy of its own.

The board's EEPROM would be the right home for a machine expected to run
standalone. This one does not: the loop only runs while a host is driving it.

Keyed by MCU serial, because gains belong to a rig rather than to whoever is
sitting in front of it -- and a second Teensy on the bench should not silently
inherit the first one's tuning.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import protocol as p

#: Fields worth keeping. Everything here describes how the loop *behaves*.
#:
#: Deliberately excluded: mode and setpoint, which are what the bench is doing
#: right now rather than how it is tuned. Restoring those on connect would
#: mean opening a terminal could start the pump, which is not a thing a
#: configuration file should be able to do. Also excluded is the calibration,
#: which belongs to BENCH_CONFIG and would be dangerous to shadow here.
#: Of those, the ones that are not floats. Casting everything to float would
#: hand the packer a float where the wire format wants a uint32, which fails
#: at pack() time rather than at load time -- a long way from the cause.
INT_FIELDS = frozenset({"period_us"})

TUNED_FIELDS = (
    "kp",
    "ki",
    "kd",
    "filter_tau_s",
    "deadband",
    "setpoint_slew",
    "output_slew",
    "out_min",
    "out_max",
    "period_us",
)

#: Repo root, found relative to this file so the path does not depend on the
#: working directory. Overridable for tests and for anyone keeping bench data
#: elsewhere.
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "tuning.json"


def default_path() -> Path:
    override = os.environ.get("NGS_TUNING_FILE")
    return Path(override) if override else DEFAULT_PATH


@dataclass(frozen=True, slots=True)
class TuningRecord:
    """One saved tuning, plus enough provenance to judge it later."""

    values: dict[str, float]
    updated: str = ""
    source: str = "manual"
    #: Autotune only: what the experiment measured, so a suspicious set of
    #: gains can be traced back to the run that produced them.
    ku: float | None = None
    tu: float | None = None
    spread: float | None = None
    note: str = ""

    def describe(self) -> str:
        parts = [f"kp {self.values.get('kp', 0):g}", f"ki {self.values.get('ki', 0):g}"]
        if self.values.get("kd"):
            parts.append(f"kd {self.values['kd']:g}")
        text = "  ".join(parts)
        if self.updated:
            text += f"   ({self.source}, {self.updated})"
        if self.ku:
            text += f"   Ku {self.ku:.4g}, Tu {self.tu:.3g} s"
            if self.spread is not None and self.spread >= 0.20:
                text += f", cycle spread {self.spread * 100:.0f} %"
        return text


class TuningStore:
    """A JSON file of tuning records, keyed by MCU serial.

    Every failure mode here ends in "carry on with the configured defaults".
    A bench tool that will not start because its preferences file is corrupt
    is worse than one that forgets a tuning.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_path()

    # -- reading -----------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            # Unreadable or half-written. Losing a tuning is recoverable;
            # refusing to run is not.
            return {}
        return data if isinstance(data, dict) else {}

    def load(self, serial: str) -> TuningRecord | None:
        entry = self._read().get("boards", {}).get(serial)
        if not isinstance(entry, dict):
            return None

        values = {
            key: int(entry[key]) if key in INT_FIELDS else float(entry[key])
            for key in TUNED_FIELDS
            if isinstance(entry.get(key), int | float)
        }
        if not values:
            return None

        return TuningRecord(
            values=values,
            updated=str(entry.get("updated", "")),
            source=str(entry.get("source", "manual")),
            ku=entry.get("ku"),
            tu=entry.get("tu"),
            spread=entry.get("spread"),
            note=str(entry.get("note", "")),
        )

    # -- writing -----------------------------------------------------------

    def save(
        self,
        serial: str,
        cfg: p.ControlCfg,
        *,
        source: str = "manual",
        ku: float | None = None,
        tu: float | None = None,
        spread: float | None = None,
        note: str = "",
    ) -> None:
        data = self._read()
        boards = data.setdefault("boards", {})

        entry: dict[str, Any] = {key: getattr(cfg, key) for key in TUNED_FIELDS}
        entry["updated"] = datetime.now().isoformat(timespec="seconds")
        entry["source"] = source
        if ku is not None:
            entry.update(ku=ku, tu=tu, spread=spread)
        if note:
            entry["note"] = note
        boards[serial] = entry

        data.setdefault(
            "_comment",
            "Tuning per board, keyed by MCU serial. Written by ngs; safe to "
            "edit, commit, and review in a diff.",
        )

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Written whole and moved into place: a half-written file here
            # would be read back as "no tuning" on the next run, silently
            # throwing away an autotune.
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            # A read-only checkout should not take the bench down with it.
            pass

    def forget(self, serial: str) -> bool:
        """Drop a board's tuning, so it falls back to the configured defaults."""
        data = self._read()
        if data.get("boards", {}).pop(serial, None) is None:
            return False
        try:
            text = json.dumps(data, indent=2, sort_keys=True) + "\n"
            self.path.write_text(text, encoding="utf-8")
        except OSError:
            return False
        return True


def apply_record(cfg: p.ControlCfg, record: TuningRecord) -> p.ControlCfg:
    """Overlay a saved tuning onto a configuration.

    Only the fields the record actually carries: an older file that predates a
    setting should leave that setting at its configured default rather than
    zeroing it.
    """
    return replace(cfg, **record.values)
