"""Fails if protocol.py and the C headers drift apart.

ngs_protocol.h says it is the single source of truth. This is what makes that
claim enforceable: it parses the headers and compares every constant and every
struct field against the Python mirror. A message added to the firmware and
forgotten here fails the host test run, not the bench session at 2am.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ngs_host import link
from ngs_host import protocol as p

FIRMWARE = Path(__file__).resolve().parents[2] / "firmware" / "lib" / "ngs"
PROTOCOL_H = FIRMWARE / "ngs_protocol.h"
BOARD_H = FIRMWARE / "ngs_board.h"
LINK_H = FIRMWARE / "ngs_link.h"


def strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)


def parse_defines(path: Path) -> dict[str, int]:
    """Numeric `#define NGS_x 0x12u` only. Macros with arguments and
    expression-valued defines are skipped -- they have no Python counterpart."""
    text = strip_comments(path.read_text(encoding="utf-8"))
    out = {}
    for name, value in re.findall(r"^#define\s+(NGS_\w+)\s+(0[xX][0-9a-fA-F]+|\d+)[uU]?\s*$",
                                  text, flags=re.MULTILINE):
        out[name] = int(value, 0)
    return out


def parse_structs(path: Path) -> dict[str, tuple[tuple[str, str], ...]]:
    """typedef struct NGS_PACKED { ... } Name; -> ((c_type, field), ...)"""
    text = strip_comments(path.read_text(encoding="utf-8"))
    out = {}
    for body, name in re.findall(
        r"typedef\s+struct\s+NGS_PACKED\s*\{(.*?)\}\s*(\w+)\s*;", text, flags=re.DOTALL
    ):
        fields = []
        for ctype, field, count in re.findall(
            r"(\w+)\s+(\w+)\s*(?:\[\s*(\d+)\s*\])?\s*;", body
        ):
            fields.append((f"{ctype}[{count}]" if count else ctype, field))
        out[name] = tuple(fields)
    return out


@pytest.fixture(scope="module")
def defines() -> dict[str, int]:
    return parse_defines(PROTOCOL_H) | parse_defines(BOARD_H) | parse_defines(LINK_H)


@pytest.fixture(scope="module")
def structs() -> dict[str, tuple[tuple[str, str], ...]]:
    return parse_structs(PROTOCOL_H)


def test_headers_are_where_we_think_they_are():
    assert PROTOCOL_H.is_file(), f"{PROTOCOL_H} missing -- did the firmware tree move?"
    assert BOARD_H.is_file()


def expected_constants() -> dict[str, int]:
    return {
        "NGS_PROTO_VERSION": p.PROTO_VERSION,
        "NGS_MAX_PAYLOAD": p.MAX_PAYLOAD,
        "NGS_MSG_RESP": p.MSG_RESP,
        "NGS_MAX_DIGITAL_PIN": p.MAX_DIGITAL_PIN,
        "NGS_MAX_ADC_CHANNEL": p.MAX_ADC_CHANNEL,
        "NGS_HEADER_SIZE": link.HEADER_SIZE,
        "NGS_CRC_SIZE": link.CRC_SIZE,
        **{f"NGS_MSG_{m.name}": int(m) for m in p.MsgType},
        **{f"NGS_ERR_{e.name}": int(e) for e in p.ErrCode},
        **{f"NGS_PIN_MODE_{m.name}": int(m) for m in p.PinMode},
    }


def test_every_python_constant_matches_the_header(defines):
    mismatched = {
        name: (value, defines.get(name))
        for name, value in expected_constants().items()
        if defines.get(name) != value
    }
    assert not mismatched, f"constant drift (python, C): {mismatched}"


def test_no_header_constant_is_missing_from_python(defines):
    """The direction that catches a *new* firmware message nobody mirrored."""
    known = set(expected_constants())
    # NGS_DECODE_* are decoder return codes, internal to the C API.
    prefixes = ("NGS_MSG_", "NGS_ERR_", "NGS_PIN_MODE_")
    missing = [
        name
        for name in defines
        if name.startswith(prefixes) and name not in known
    ]
    assert not missing, f"in the header but not in protocol.py: {missing}"


def payload_classes() -> list[type[p.Payload]]:
    return [cls for cls in p.Payload.__subclasses__() if cls.C_NAME]


def test_all_c_structs_have_a_python_mirror(structs):
    mirrored = {cls.C_NAME for cls in payload_classes()}
    assert set(structs) == mirrored, (
        f"only in C: {set(structs) - mirrored}; only in Python: {mirrored - set(structs)}"
    )


@pytest.mark.parametrize("cls", payload_classes(), ids=lambda c: c.C_NAME)
def test_struct_fields_match(cls, structs):
    """Types, names and order -- not just the total size, which would let two
    compensating mistakes through."""
    assert structs[cls.C_NAME] == cls.FIELDS, f"{cls.C_NAME} layout drifted"


@pytest.mark.parametrize("cls", payload_classes(), ids=lambda c: c.C_NAME)
def test_struct_size_is_what_the_c_compiler_would_produce(cls, structs):
    sizes = {
        "uint8_t": 1,
        "int8_t": 1,
        "uint16_t": 2,
        "int16_t": 2,
        "uint32_t": 4,
        "int32_t": 4,
        "float": 4,
    }
    expected = 0
    for ctype, _name in structs[cls.C_NAME]:
        base, _, count = ctype.partition("[")
        expected += sizes[base] * (int(count.rstrip("]")) if count else 1)
    assert cls.size() == expected


def test_frame_overhead_matches_the_c_constants(defines):
    expected = defines["NGS_HEADER_SIZE"] + p.MAX_PAYLOAD + defines["NGS_CRC_SIZE"]
    assert expected == link.FRAME_RAW_MAX
