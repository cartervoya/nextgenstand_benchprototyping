"""Web front-end tests.

The page is served from a string and the state is a plain dict, so both can be
checked without a browser. What is not covered here is the JavaScript.
"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from ngs_host.bench import Bench
from ngs_host.device import Device
from ngs_host.fake import FakeBoard, FakeDevice
from ngs_host.web import PAGE, WebBench, serve, snapshot_to_dict


@pytest.fixture
def board() -> FakeBoard:
    return FakeBoard()


@pytest.fixture
def web(board: FakeBoard) -> WebBench:
    bench = Bench(Device(FakeDevice(board)))
    bench.initialize()
    return WebBench(bench, port="sim", fw="0.1.0")


def test_state_contains_every_channel(web):
    state = web.state()
    assert {c["code"] for c in state["channels"]} == {"P", "V1", "V2", "F"}
    assert state["status"]["rx_crc_errors"] == 0
    assert state["error"] is None


def test_state_is_json_serialisable(web):
    """It goes over the wire as JSON; a stray dataclass would 500 the page."""
    json.dumps(web.state())


def test_a_command_runs_and_returns_fresh_state(web, board):
    result = web.command("V1O;P50;")
    assert [r["ok"] for r in result["results"]] == [True, True]
    assert board.pin_values[32] == 1

    valve = next(c for c in result["state"]["channels"] if c["code"] == "V1")
    assert valve["state"] == "open"


def test_a_bad_command_is_reported_not_raised(web):
    result = web.command("P900")
    assert result["results"][0]["ok"] is False
    assert "0-100" in result["results"][0]["text"]


def test_a_mismatch_reaches_the_page(web, board):
    web.command("V1O")
    board.pin_values.clear()

    state = web.state()
    assert state["mismatch"] == ["V1"]
    assert next(c for c in state["channels"] if c["code"] == "V1")["mismatch"] is True


def test_a_link_error_reaches_the_page(web):
    class Dead:
        def read(self, size=1):
            raise OSError("device disconnected")

        def write(self, data):
            raise OSError("device disconnected")

        def close(self):
            pass

    web.bench.device._t = Dead()
    assert "disconnected" in web.state()["error"]


def test_help_placeholder_is_substituted(web):
    """The page embeds the live help; an unsubstituted marker would be a
    JavaScript syntax error and a blank screen."""
    assert "__HELP__" in PAGE
    page = PAGE.replace("__HELP__", json.dumps("V1O / V1C"))
    assert "__HELP__" not in page
    assert "V1O / V1C" in page


def test_snapshot_to_dict_handles_a_failed_poll(web):
    from ngs_host.bench import Snapshot

    state = snapshot_to_dict(Snapshot(monotonic=0.0, error="boom"), "COM3", "0.1.0")
    assert state["error"] == "boom"
    assert state["channels"] == []
    assert state["status"] is None


# -- served over HTTP ------------------------------------------------------


@pytest.fixture
def server(web: WebBench):
    httpd = serve(web, port=0)  # port 0: let the OS pick a free one
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_serves_the_page(server):
    with urllib.request.urlopen(f"{server}/", timeout=5) as r:
        body = r.read().decode()
    assert r.status == 200
    assert "NextGen Stand bench" in body
    assert "__HELP__" not in body


def test_serves_state_as_json(server):
    with urllib.request.urlopen(f"{server}/api/state", timeout=5) as r:
        state = json.loads(r.read())
    assert state["port"] == "sim"
    assert len(state["channels"]) == 4


def test_accepts_a_command_over_http(server, board):
    req = urllib.request.Request(
        f"{server}/api/command",
        data=json.dumps({"line": "V2O"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        result = json.loads(r.read())
    assert result["results"][0]["ok"]
    assert board.pin_values[31] == 1


def test_unknown_paths_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{server}/nope", timeout=5)
    assert exc.value.code == 404


def test_it_listens_on_localhost_only(web):
    """This drives hardware; it has no business on the network."""
    httpd = serve(web, port=0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


# -- live plotting ---------------------------------------------------------


def test_polling_the_state_records_a_sample(web):
    """One poll feeds both the readouts and the plot: the trace is exactly
    what was displayed, and the bench is not asked twice."""
    before = web.history.count
    web.state()
    assert web.history.count == before + 1


def test_traces_are_offered_to_the_page(web):
    keys = {t["key"] for t in web.traces()}
    assert {"flow", "pump_output", "pump_setpoint"} <= keys
    assert all({"key", "label", "unit", "axis", "color"} <= set(t) for t in web.traces())


def test_history_is_served_incrementally(server):
    for _ in range(6):
        urllib.request.urlopen(f"{server}/api/state", timeout=5).read()

    first = json.loads(urllib.request.urlopen(f"{server}/api/history?since=0", timeout=5).read())
    assert len(first["t"]) >= 6

    again = json.loads(
        urllib.request.urlopen(f"{server}/api/history?since={first['seq']}", timeout=5).read()
    )
    assert again["t"] == [], "a caught-up client must not re-download the history"


def test_the_overview_is_fixed_size_whatever_is_stored(server, web):
    for _ in range(50):
        web.state()

    data = json.loads(
        urllib.request.urlopen(f"{server}/api/history?mode=overview&buckets=8", timeout=5).read()
    )
    assert len(data["t"]) == 8
    assert data["decimated"] is True
    # min/max pairs, so a spike inside a bucket survives
    assert all(len(pair) == 2 for pair in data["series"]["flow"])


def test_a_client_can_ask_for_only_the_traces_it_draws(server, web):
    web.state()
    data = json.loads(
        urllib.request.urlopen(f"{server}/api/history?since=0&keys=flow", timeout=5).read()
    )
    assert list(data["series"]) == ["flow"]


def test_the_plotting_code_is_served_locally(server):
    """A bench tool that needs a CDN to draw a graph does not work on a bench
    with no internet, which is most of them."""
    with urllib.request.urlopen(f"{server}/plot.js", timeout=5) as r:
        js = r.read().decode()
    assert r.headers["Content-Type"].startswith("application/javascript")
    assert "class Ring" in js and "class Plot" in js


def test_the_page_carries_no_external_references(server):
    with urllib.request.urlopen(f"{server}/", timeout=5) as r:
        body = r.read().decode()
    for marker in ("http://", "https://", "cdn."):
        assert marker not in body.replace("http://127.0.0.1", ""), f"external reference: {marker}"


def test_a_faulted_reading_becomes_a_gap_not_a_number(web, board):
    """Plotting a fault as a value would draw a flow that was never measured."""
    board.adc = lambda channel: 0
    web.state()

    data = web.history.since(0, keys=["flow"])
    assert data["series"]["flow"][-1] is None
