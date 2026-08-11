"""The dashboard as a local web page, for a second screen or a popped-out window.

Same Bench, same command language as the terminal UI -- this is a different
front end, not a different program. Anything added to BENCH_CONFIG appears
here too, for the same reason it appears in the TUI.

Deliberately stdlib-only (`http.server`) rather than FastAPI or Flask: a bench
tool that needs a working web stack to talk to a valve is a bench tool that
stops working at the worst moment. The page is a single self-contained string,
so there is nothing to build and nothing to serve from disk.

Concurrency: the server is single-threaded on purpose. A serial link is one
conversation at a time, and `Device` is not thread-safe -- serialising requests
in the server is simpler and more honest than bolting a lock onto the driver.
Each request is a few milliseconds, so a 2 Hz poll and the odd command never
queue noticeably.

Binds to localhost only. This drives real hardware; it has no business
listening on the network.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .bench import Bench, Snapshot
from .commands import execute_line, help_rows
from .history import History, sample_from, traces_for

DEFAULT_PORT = 8765
HOST = "127.0.0.1"


def snapshot_to_dict(snapshot: Snapshot, port: str, fw: str) -> dict[str, Any]:
    """The wire format for the page. Flat and boring on purpose -- the browser
    should render values, not re-derive them."""
    return {
        "port": port,
        "fw": fw,
        "error": snapshot.error,
        "mismatch": [r.spec.code for r in snapshot.mismatched_valves],
        "channels": [
            *[
                {
                    "code": r.spec.code,
                    "name": r.spec.description,
                    "value": (
                        f"{snapshot.control.setpoint_target:.0f} "
                        f"{snapshot.control.mode_name} ({r.percent:.1f} %)"
                        if snapshot.control is not None and snapshot.control.mode != 0
                        else r.text
                    ),
                    "kind": "pwm",
                    "detail": f"pin {r.spec.pin}, {r.spec.freq_hz / 1000:g} kHz, "
                    f"{r.spec.resolution}-bit",
                }
                for r in snapshot.pwms.values()
            ],
            *[
                {
                    "code": r.spec.code,
                    "name": r.spec.description,
                    "value": r.text,
                    "kind": "valve",
                    "state": "open" if r.is_open else "closed",
                    "mismatch": r.mismatch,
                    "detail": f"pin {r.spec.pin}",
                }
                for r in snapshot.valves.values()
            ],
            *[
                {
                    "code": r.spec.code,
                    "name": r.spec.description,
                    "value": r.text,
                    "kind": "analog",
                    "faulted": r.faulted,
                    "detail": f"pin {r.spec.pin}, {r.volts:.3f} V, raw {r.raw}",
                }
                for r in snapshot.analogs.values()
            ],
        ],
        "control": None
        if snapshot.control is None
        else {
            "mode": snapshot.control.mode_name,
            "auto": snapshot.control.mode != 0,
            "setpoint": round(snapshot.control.setpoint_target, 1),
            "setpoint_now": round(snapshot.control.setpoint, 1),
            "measurement": round(snapshot.control.measurement, 1),
            "error": round(snapshot.control.error, 1),
            "output": round(snapshot.control.output, 1),
            "p": round(snapshot.control.p_term, 2),
            "i": round(snapshot.control.i_term, 2),
            "d": round(snapshot.control.d_term, 2),
            "flags": snapshot.control.flag_names(),
            "faults": snapshot.control.fault_count,
        },
        "estop": None
        if snapshot.status is None
        else {
            "latched": snapshot.status.estopped,
            "source": snapshot.status.estop_source_name.lower(),
            "safe_entries": snapshot.status.safe_entries,
        },
        "status": None
        if snapshot.status is None
        else {
            "uptime_s": round(snapshot.status.uptime_us / 1e6, 1),
            "rx_frames": snapshot.status.rx_frames,
            "tx_frames": snapshot.status.tx_frames,
            "rx_crc_errors": snapshot.status.rx_crc_errors,
            "rx_overflows": snapshot.status.rx_overflows,
            "loop_max_us": snapshot.status.loop_max_us,
            "temp_c": round(snapshot.status.temp_c, 1),
        },
    }


@dataclass
class WebBench:
    """The bench plus the bits of identity the page displays."""

    bench: Bench
    port: str
    fw: str = "?"
    history: History | None = None
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.history is None:
            self.history = History(traces=traces_for(self.bench.config))

    def state(self) -> dict[str, Any]:
        """Poll, record, and report.

        Recording happens here rather than on a timer of its own: one poll
        feeds both the readouts and the plot, so the trace is exactly what was
        displayed and the bench is not asked the same questions twice.
        """
        snapshot = self.bench.poll()
        self.history.record(
            snapshot.monotonic - self.started, sample_from(snapshot, self.bench.config)
        )
        return snapshot_to_dict(snapshot, self.port, self.fw)

    def traces(self) -> list[dict[str, Any]]:
        return [
            {
                "key": t.key,
                "label": t.label,
                "unit": t.unit,
                "axis": t.axis,
                "color": t.color,
            }
            for t in self.history.traces
        ]

    def command(self, line: str) -> dict[str, Any]:
        results = execute_line(self.bench, line)
        return {
            "results": [
                {"ok": r.ok, "text": r.text, "show_status": r.show_status} for r in results
            ],
            "state": self.state(),
        }


def make_handler(web: WebBench) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Quiet: one log line per 2 Hz poll would bury anything worth reading.
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: dict[str, Any], code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's API
            if self.path in ("/", "/index.html"):
                rows = [[r.syntax, r.description] for r in help_rows(web.bench)]
                page = PAGE.replace("__HELP__", json.dumps(rows))
                page = page.replace("__TRACES__", json.dumps(web.traces()))
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send_json(web.state())
            elif self.path.startswith("/api/history"):
                self._send_json(self._history(web))
            elif self.path == "/plot.js":
                self._send(200, PLOT_JS.encode(), "application/javascript")
            else:
                self._send_json({"error": "not found"}, 404)

        def _history(self, web: WebBench) -> dict[str, Any]:
            """Incremental by default, decimated overview on request.

            `since` is the client's cursor. A caught-up client gets the two or
            three samples that are new; a fresh one asks for the overview and
            gets the whole trace in a fixed-size payload.
            """
            query = parse_qs(urlparse(self.path).query)
            keys = query.get("keys", [""])[0]
            keys = [k for k in keys.split(",") if k] or None

            if query.get("mode", [""])[0] == "overview":
                buckets = min(int(query.get("buckets", ["1200"])[0] or 1200), 4000)
                return web.history.decimate(keys=keys, buckets=buckets)

            since = int(query.get("since", ["0"])[0] or 0)
            return web.history.since(since, keys=keys)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/command":
                self._send_json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "bad JSON"}, 400)
                return
            self._send_json(web.command(str(payload.get("line", ""))))

    return Handler


def serve(web: WebBench, port: int = DEFAULT_PORT) -> HTTPServer:
    """Build the server. The caller runs it, so it can print the URL first."""
    return HTTPServer((HOST, port), make_handler(web))


#: Served as its own file rather than inlined, so the browser caches it and
#: it stays readable as JavaScript instead of as a Python string.
PLOT_JS = (Path(__file__).with_name("plot.js")).read_text(encoding="utf-8")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>NextGen Stand bench</title>
<style>
  :root { color-scheme: dark; --bg:#11131a; --fg:#d8dee9; --dim:#727a8c;
          --ok:#a3d977; --warn:#e5c07b; --bad:#e06c75; --accent:#61afef;
          --panel:#171a23; --line:#252a36; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-monospace, "Cascadia Code", Consolas, monospace; }
  main { max-width:1180px; margin:0 auto; padding:16px;
         display:grid; grid-template-columns:minmax(0,1fr) 320px; gap:16px;
         align-items:start; }
  .content { min-width:0; }
  /* The dock stays put while the log scrolls -- the whole point. */
  aside { position:sticky; top:16px; background:var(--panel); border:1px solid var(--line);
          border-radius:6px; padding:10px 12px; }
  aside h2 { font-size:11px; text-transform:uppercase; letter-spacing:1px;
             color:var(--dim); margin:0 0 8px; font-weight:700; }
  /* Deliberately smaller than the readouts: this is reference, not data. */
  aside table { font-size:11.5px; line-height:1.45; }
  aside td { padding:1px 0; border:0; vertical-align:top; }
  aside td.k { color:var(--accent); font-weight:700; white-space:nowrap; padding-right:10px; }
  aside td.v { color:var(--dim); }
  aside .foot { margin-top:8px; padding-top:8px; border-top:1px solid var(--line);
                font-size:11px; color:var(--dim); }
  @media (max-width:900px) {
    main { grid-template-columns:minmax(0,1fr); }
    aside { position:static; }
  }
  h1 { font-size:15px; margin:0 0 12px; color:var(--dim); font-weight:600; }
  .bar { padding:8px 12px; border:1px solid var(--line); border-radius:6px;
         background:var(--panel); margin-bottom:12px; }
  .bar.ok { color:var(--ok); } .bar.bad { color:var(--bad); font-weight:700; }
  .bar span { margin-right:14px; white-space:nowrap; }
  table { width:100%; border-collapse:collapse; margin-bottom:12px; }
  td { padding:6px 8px; border-bottom:1px solid var(--line); }
  td.code { color:var(--dim); width:3em; }
  td.name { font-weight:600; }
  td.val { text-align:right; white-space:nowrap; }
  td.detail { color:var(--dim); }
  .open { color:var(--ok); } .closed { color:var(--warn); }
  .num { color:var(--accent); } .bad { color:var(--bad); font-weight:700; }
  #log { background:var(--panel); border:1px solid var(--line); border-radius:6px;
         padding:8px 12px; height:190px; overflow-y:auto; white-space:pre-wrap;
         margin-bottom:12px; }
  #log div.err { color:var(--bad); } #log div.echo { color:var(--accent); }
  form { display:flex; gap:8px; }
  input { flex:1; background:var(--panel); border:1px solid var(--line);
          border-radius:6px; color:var(--fg); padding:9px 12px; font:inherit; }
  input:focus { outline:none; border-color:var(--accent); }
  button { background:var(--accent); border:0; border-radius:6px; color:#11131a;
           padding:9px 18px; font:inherit; font-weight:700; cursor:pointer; }
  .stop { background:var(--bad); color:#fff; }
  /* Deliberately the largest thing on the page. */
  .estop { width:100%; background:var(--bad); color:#fff; font-size:20px;
           font-weight:800; letter-spacing:2px; padding:16px; margin-bottom:12px;
           border:0; border-radius:8px; cursor:pointer; }
  .estop:hover { filter:brightness(1.15); }
  .estop.latched { background:#7a1620; color:#ffb3b3; }
  .loop { color:#c678dd; margin-bottom:10px; min-height:1.4em; }
  .plotwrap { border:1px solid var(--line); border-radius:6px; background:var(--panel);
              padding:8px 10px; margin-bottom:12px; }
  .plotbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
             font-size:11.5px; color:var(--dim); margin-bottom:6px; }
  .plotbar .ttl { text-transform:uppercase; letter-spacing:1px; font-weight:700; }
  .plotbar label { display:flex; gap:4px; align-items:center; }
  .plotbar select, .plotbar input, .plotbar button {
      background:var(--bg); color:var(--fg); border:1px solid var(--line);
      border-radius:4px; font:inherit; padding:2px 5px; }
  .plotbar button { cursor:pointer; }
  #plot { width:100%; height:240px; display:block; border-radius:4px; }
  .traces { display:flex; flex-wrap:wrap; gap:4px 12px; margin-top:6px; font-size:11.5px; }
  .traces label { display:flex; gap:5px; align-items:center; cursor:pointer; color:var(--dim); }
  .traces label.on { color:var(--fg); }
  .traces .sw { width:11px; height:11px; border-radius:2px; display:inline-block; }
  .loop.warn { color:var(--warn); } .loop.bad { color:var(--bad); font-weight:700; }
</style>
<script src="/plot.js"></script>
</head>
<body><main>
  <div class="content">
  <h1>NextGen Stand bench</h1>
  <button type="button" class="estop" id="estopbtn"
          title="Everything to its safe state, latched (Ctrl-E)">EMERGENCY STOP</button>
  <div class="bar ok" id="bar">connecting...</div>
  <table id="channels"></table>
  <div id="loop" class="loop"></div>

  <div class="plotwrap">
    <div class="plotbar">
      <span class="ttl">live</span>
      <label>window
        <select id="window">
          <option value="30">30 s</option>
          <option value="120" selected>2 min</option>
          <option value="600">10 min</option>
          <option value="3600">1 hour</option>
          <option value="0">everything</option>
        </select>
      </label>
      <label>left
        <input id="leftmin" class="scale" placeholder="auto" size="5">
        <input id="leftmax" class="scale" placeholder="auto" size="5">
      </label>
      <label>right
        <input id="rightmin" class="scale" placeholder="auto" size="5">
        <input id="rightmax" class="scale" placeholder="auto" size="5">
      </label>
      <button type="button" id="clearplot">clear</button>
      <span id="plotinfo" class="dim"></span>
    </div>
    <canvas id="plot" height="240"></canvas>
    <div id="traces" class="traces"></div>
  </div>
  <div id="log"></div>
  <form id="form" autocomplete="off">
    <input id="line" placeholder="V1O;P50;   -- ? for help" autofocus>
    <button type="submit">Send</button>
    <button type="button" class="stop" id="stopbtn" title="Pump to 0, valves closed">STOP</button>
  </form>
  </div>
  <aside>
    <h2>commands</h2>
    <table id="help"></table>
    <div class="foot">chain with <b>;</b> &mdash; e.g. <b>V1O;P50;</b><br>
      <b>Escape</b> = emergency stop</div>
  </aside>
</main>
<script>
const HELP = __HELP__;
const TRACES = __TRACES__;
const $ = (id) => document.getElementById(id);
let busy = false;      // one request at a time: the serial link is serial
let history = [], hpos = 0;

function log(text, cls) {
  for (const line of String(text).split("\\n")) {
    const div = document.createElement("div");
    if (cls) div.className = cls;
    div.textContent = line;
    $("log").appendChild(div);
  }
  $("log").scrollTop = $("log").scrollHeight;
}

function render(s) {
  const bar = $("bar"), btn = $("estopbtn");
  const latched = s.estop && s.estop.latched;
  btn.classList.toggle("latched", !!latched);
  btn.textContent = latched ? "LATCHED - click to clear" : "EMERGENCY STOP";

  if (latched) {
    bar.className = "bar bad";
    bar.textContent = s.port + "  *** EMERGENCY STOP LATCHED (" + s.estop.source
                    + ") ***  outputs are safe and will not move.";
    $("channels").innerHTML = channelRows(s);
    $("loop").textContent = "";
    return;
  }
  if (s.error) {
    bar.className = "bar bad";
    bar.textContent = s.port + "  LINK ERROR  " + s.error;
  } else if (s.mismatch.length) {
    bar.className = "bar bad";
    bar.textContent = s.port + "  OUTPUT MISMATCH on " + s.mismatch.join(", ")
                    + " -- did the board reset? Send Z to re-apply the safe state.";
  } else {
    bar.className = "bar ok";
    const st = s.status;
    bar.innerHTML = ["<span>" + s.port + "</span>", "<span>fw " + s.fw + "</span>"].join("")
      + (st ? ["<span>up " + st.uptime_s + "s</span>",
               "<span>rx " + st.rx_frames + "</span>",
               "<span>tx " + st.tx_frames + "</span>",
               "<span>crc-err " + st.rx_crc_errors + "</span>",
               "<span>loop-max " + st.loop_max_us + " us</span>",
               "<span>" + st.temp_c + " C</span>"].join("") : "");
  }

  const loop = $("loop"), ct = s.control;
  if (!ct || !ct.auto) {
    loop.className = "loop";
    loop.textContent = ct && ct.faults
      ? "loop: manual  (dropped out on a sensor fault " + ct.faults + "x)" : "";
  } else {
    loop.className = "loop" + (ct.flags.includes("FAULT") ? " bad"
                    : (ct.flags.length ? " warn" : ""));
    loop.textContent = "loop: " + ct.mode + "   sp " + ct.setpoint + "   meas " + ct.measurement
      + "   err " + ct.error + "   P " + ct.p + "   I " + ct.i
      + (ct.d ? "   D " + ct.d : "")
      + (ct.flags.length ? "   [" + ct.flags.join(" ") + "]" : "");
  }

  $("channels").innerHTML = channelRows(s);
}

function channelRows(s) {
  return s.channels.map(c => {
    let cls = "num";
    if (c.kind === "valve") cls = c.mismatch ? "bad" : (c.state === "open" ? "open" : "closed");
    if (c.kind === "analog" && c.faulted) cls = "bad";
    return "<tr><td class='code'>" + c.code + "</td><td class='name'>" + c.name
         + "</td><td class='val " + cls + "'>" + c.value
         + "</td><td class='detail'>" + c.detail + "</td></tr>";
  }).join("");
}

async function poll() {
  if (busy) return;                       // never overlap with a command
  try {
    const r = await fetch("/api/state");
    render(await r.json());
  } catch (e) {
    $("bar").className = "bar bad";
    $("bar").textContent = "host unreachable -- is `ngs web` still running?";
  }
}

async function send(line) {
  if (!line.trim() || busy) return;
  if (line.trim() === "?") { log("commands are listed in the panel on the right"); return; }
  busy = true;
  log("> " + line, "echo");
  history.push(line); hpos = history.length;
  try {
    const r = await fetch("/api/command", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({line})
    });
    const data = await r.json();
    for (const res of data.results) {
      if (res.show_status) log(JSON.stringify(data.state.status));
      else if (res.text) log(res.text, res.ok ? null : "err");
    }
    render(data.state);
  } catch (e) {
    log("host unreachable: " + e, "err");
  } finally { busy = false; }
}

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  send($("line").value);
  $("line").value = "";
});
$("stopbtn").addEventListener("click", () => send("X"));
$("estopbtn").addEventListener("click", () => send(
  $("estopbtn").classList.contains("latched") ? "EC" : "!"));
// Ctrl-E, matching the terminal dashboard. NOT Escape: people press Escape to
// dismiss and clear things, and it clears the input line in the terminal UI --
// binding a latching emergency stop to it made it a hair trigger, which is
// exactly how this ended up latched by accident.
document.addEventListener("keydown", (e) => {
  if (e.key === "e" && e.ctrlKey) { e.preventDefault(); send("!"); }
  if (e.key === "Escape") { $("line").value = ""; }
});
$("line").addEventListener("keydown", (e) => {   // arrow-key history
  if (e.key === "ArrowUp" && hpos > 0) { $("line").value = history[--hpos]; e.preventDefault(); }
  if (e.key === "ArrowDown") {
    hpos = Math.min(hpos + 1, history.length);
    $("line").value = hpos === history.length ? "" : history[hpos];
    e.preventDefault();
  }
});

$("help").innerHTML = HELP.map(([k, v]) =>
  "<tr><td class='k'>" + k + "</td><td class='v'>" + v + "</td></tr>").join("");
log("ready -- commands are listed on the right");

/* ---- live plot ------------------------------------------------------- */
const ring = new Ring(200000, TRACES.map(t => t.key));
const plot = new Plot($("plot"), {
  specs: TRACES,
  /* A sensible default rather than everything: eight traces at once is a
     mess, and flow plus what is driving it is what you actually watch. */
  enabled: TRACES.filter(t => ["flow", "pump_output", "pump_setpoint"].includes(t.key))
                 .map(t => t.key),
  window: 120,
});
plot.setData(ring);

$("traces").innerHTML = TRACES.map(t =>
  "<label class='" + (plot.enabled.has(t.key) ? "on" : "") + "' data-key='" + t.key + "'>"
  + "<input type='checkbox' " + (plot.enabled.has(t.key) ? "checked" : "") + ">"
  + "<span class='sw' style='background:" + t.color + "'></span>"
  + t.label + (t.unit ? " (" + t.unit + ")" : "") + "</label>").join("");

$("traces").addEventListener("change", (e) => {
  const label = e.target.closest("label"), key = label.dataset.key;
  if (e.target.checked) plot.enabled.add(key); else plot.enabled.delete(key);
  label.classList.toggle("on", e.target.checked);
  plot.draw();
});

$("window").addEventListener("change", () => {
  const v = Number($("window").value);
  /* 0 means "everything": widen to whatever the buffer actually spans. */
  plot.window = v || Math.max(1, ring.t[(ring.cursor - 1 + ring.capacity) % ring.capacity]);
  plot.draw();
});

function readScale(axis) {
  const lo = parseFloat($(axis + "min").value), hi = parseFloat($(axis + "max").value);
  /* Both ends or neither: half a manual range is ambiguous, and silently
     auto-scaling the other half makes the plot lie about what you asked for. */
  plot.manual[axis] = (Number.isFinite(lo) && Number.isFinite(hi) && hi > lo) ? [lo, hi] : null;
  plot.draw();
}
for (const id of ["leftmin", "leftmax", "rightmin", "rightmax"]) {
  $(id).addEventListener("change", () => readScale(id.startsWith("left") ? "left" : "right"));
}

$("clearplot").addEventListener("click", async () => {
  ring.clear();
  const r = await fetch("/api/history?mode=overview&buckets=1");
  ring.seq = (await r.json()).seq;   // start from now, not from the old data
  plot.draw();
});

async function pollHistory() {
  try {
    const r = await fetch("/api/history?since=" + ring.seq);
    const d = await r.json();
    if (d.t.length) {
      for (let i = 0; i < d.t.length; i++) {
        const values = {};
        for (const k in d.series) values[k] = d.series[k][i];
        ring.push(d.t[i], values);
      }
      ring.seq = d.seq;
      plot.draw();
      $("plotinfo").textContent = ring.held() + " points buffered";
    }
  } catch (e) { /* the state poll already reports an unreachable host */ }
}

window.addEventListener("resize", () => plot.draw());

poll();
pollHistory();
setInterval(poll, 500);    // 2 Hz, matching the terminal dashboard
/* Faster than the readouts: the plot is the thing you watch for shape, and
   the transfer is only the samples that are new. */
setInterval(pollHistory, 250);
</script>
</body></html>
"""
