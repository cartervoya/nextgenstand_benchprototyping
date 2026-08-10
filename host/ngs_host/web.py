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
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .bench import Bench, Snapshot
from .commands import execute_line, help_text

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
                    "value": r.text,
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

    def state(self) -> dict[str, Any]:
        return snapshot_to_dict(self.bench.poll(), self.port, self.fw)

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
                page = PAGE.replace("__HELP__", json.dumps(help_text(web.bench)))
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send_json(web.state())
            else:
                self._send_json({"error": "not found"}, 404)

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
  main { max-width:900px; margin:0 auto; padding:16px; }
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
</style>
</head>
<body><main>
  <h1>NextGen Stand bench</h1>
  <div class="bar ok" id="bar">connecting...</div>
  <table id="channels"></table>
  <div id="log"></div>
  <form id="form" autocomplete="off">
    <input id="line" placeholder="V1O;P50;   -- ? for help" autofocus>
    <button type="submit">Send</button>
    <button type="button" class="stop" id="stopbtn" title="Pump to 0, valves closed">STOP</button>
  </form>
</main>
<script>
const HELP = __HELP__;
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
  const bar = $("bar");
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

  $("channels").innerHTML = s.channels.map(c => {
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
  if (line.trim() === "?") { log(HELP); return; }
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
$("line").addEventListener("keydown", (e) => {   // arrow-key history
  if (e.key === "ArrowUp" && hpos > 0) { $("line").value = history[--hpos]; e.preventDefault(); }
  if (e.key === "ArrowDown") {
    hpos = Math.min(hpos + 1, history.length);
    $("line").value = hpos === history.length ? "" : history[hpos];
    e.preventDefault();
  }
});

log(HELP);
poll();
setInterval(poll, 500);    // 2 Hz, matching the terminal dashboard
</script>
</body></html>
"""
