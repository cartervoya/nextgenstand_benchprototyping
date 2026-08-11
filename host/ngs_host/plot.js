/* Live plotting for the bench dashboard.
 *
 * Canvas rather than SVG or a charting library: a chart library builds a DOM
 * node per point and falls over somewhere around ten thousand of them, and
 * this has to stay smooth with a hundred thousand while the bench runs all
 * afternoon. Drawing a polyline into a canvas is one path per trace no matter
 * how many points are in it.
 *
 * The client keeps its own ring buffer and asks the host only for what is new,
 * so a steady-state poll moves a handful of samples rather than the whole
 * history. On attach it takes a decimated overview instead -- fixed size,
 * whatever is stored.
 *
 * Nothing is loaded from the network: a bench tool that needs a CDN to draw a
 * graph does not work on a bench with no internet, which is most of them.
 */

class Ring {
  /* Fixed-size, preallocated. Same reasoning as the host side: no allocation
   * per sample, no shifting, memory decided once. */
  constructor(capacity, keys) {
    this.capacity = capacity;
    this.keys = keys;
    this.t = new Float64Array(capacity);
    this.v = {};
    for (const k of keys) this.v[k] = new Float64Array(capacity).fill(NaN);
    this.count = 0;
    this.cursor = 0;
    this.seq = 0;
  }

  push(t, values) {
    const i = this.cursor;
    this.t[i] = t;
    for (const k of this.keys) {
      const val = values[k];
      this.v[k][i] = (val === null || val === undefined) ? NaN : val;
    }
    this.cursor = (this.cursor + 1) % this.capacity;
    this.count++;
  }

  /* Oldest-to-newest index walk, without copying. */
  *indices() {
    const held = Math.min(this.count, this.capacity);
    let i = (this.cursor - held + this.capacity) % this.capacity;
    for (let n = 0; n < held; n++) {
      yield i;
      i = (i + 1) % this.capacity;
    }
  }

  held() { return Math.min(this.count, this.capacity); }

  clear() {
    this.count = 0; this.cursor = 0; this.seq = 0;
    for (const k of this.keys) this.v[k].fill(NaN);
  }
}

class Plot {
  constructor(canvas, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: false });
    this.specs = opts.specs;            // [{key,label,unit,axis,color}]
    this.enabled = new Set(opts.enabled);
    this.window = opts.window || 120;   // seconds shown
    this.manual = { left: null, right: null };  // null = auto-scale
    this.data = null;
  }

  setData(ring) { this.data = ring; }

  /* Auto-scale is over what is *visible*, not the whole buffer: a spike an
   * hour ago should not flatten the last minute. A little padding keeps a
   * trace off the frame edge, and a floor stops a dead-flat signal from being
   * magnified into meaningless noise. */
  range(axis, from, to) {
    if (this.manual[axis]) return this.manual[axis];

    let lo = Infinity, hi = -Infinity;
    const ring = this.data;
    for (const spec of this.specs) {
      if (spec.axis !== axis || !this.enabled.has(spec.key)) continue;
      const col = ring.v[spec.key];
      for (const i of ring.indices()) {
        const t = ring.t[i];
        if (t < from || t > to) continue;
        const val = col[i];
        if (!Number.isFinite(val)) continue;
        if (val < lo) lo = val;
        if (val > hi) hi = val;
      }
    }
    if (!Number.isFinite(lo)) return [0, 1];
    if (hi - lo < 1e-9) { lo -= 0.5; hi += 0.5; }
    const pad = (hi - lo) * 0.08;
    return [lo - pad, hi + pad];
  }

  draw() {
    const ctx = this.ctx, ring = this.data;
    const dpr = window.devicePixelRatio || 1;
    const w = this.canvas.clientWidth, h = this.canvas.clientHeight;
    if (this.canvas.width !== w * dpr || this.canvas.height !== h * dpr) {
      this.canvas.width = w * dpr;
      this.canvas.height = h * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const style = getComputedStyle(document.body);
    const bg = style.getPropertyValue("--panel").trim() || "#171a23";
    const grid = style.getPropertyValue("--line").trim() || "#252a36";
    const dim = style.getPropertyValue("--dim").trim() || "#727a8c";

    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, h);

    const pad = { l: 52, r: 52, t: 8, b: 18 };
    const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
    if (pw <= 0 || ph <= 0 || ring.held() === 0) return;

    const held = ring.held();
    const newest = ring.t[(ring.cursor - 1 + ring.capacity) % ring.capacity];
    const to = newest;
    const from = to - this.window;

    const left = this.range("left", from, to);
    const right = this.range("right", from, to);

    /* Grid and axis labels. */
    ctx.strokeStyle = grid; ctx.lineWidth = 1;
    ctx.fillStyle = dim; ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const y = Math.round(pad.t + (ph * i) / 4) + 0.5;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + pw, y); ctx.stroke();

      const lv = left[1] - (left[1] - left[0]) * (i / 4);
      const rv = right[1] - (right[1] - right[0]) * (i / 4);
      ctx.textAlign = "right"; ctx.fillText(fmt(lv), pad.l - 6, y);
      ctx.textAlign = "left";  ctx.fillText(fmt(rv), pad.l + pw + 6, y);
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (let i = 0; i <= 4; i++) {
      const x = Math.round(pad.l + (pw * i) / 4) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ph); ctx.stroke();
      const secs = this.window * (1 - i / 4);
      ctx.fillText(secs === 0 ? "now" : "-" + fmtTime(secs), x, pad.t + ph + 4);
    }

    /* One path per trace. Non-finite samples break the path rather than
     * bridging it: a gap in the data must look like a gap. */
    const xOf = (t) => pad.l + ((t - from) / (to - from || 1)) * pw;

    for (const spec of this.specs) {
      if (!this.enabled.has(spec.key)) continue;
      const rng = spec.axis === "right" ? right : left;
      const span = rng[1] - rng[0] || 1;
      const yOf = (v) => pad.t + ph - ((v - rng[0]) / span) * ph;
      const col = ring.v[spec.key];

      ctx.beginPath();
      ctx.strokeStyle = spec.color;
      ctx.lineWidth = 1.5;
      let drawing = false;
      let lastX = -1e9;

      for (const i of ring.indices()) {
        const t = ring.t[i];
        if (t < from || !Number.isFinite(t)) continue;
        const val = col[i];
        if (!Number.isFinite(val)) { drawing = false; continue; }

        const x = xOf(t), y = yOf(val);
        /* Skip sub-pixel steps: at a hundred thousand points most samples land
         * on a pixel already drawn, and lineTo for each is the difference
         * between 60 fps and 6. */
        if (drawing && x - lastX < 0.5) continue;
        if (drawing) ctx.lineTo(x, y); else { ctx.moveTo(x, y); drawing = true; }
        lastX = x;
      }
      ctx.stroke();
    }

    /* Point count, so it is obvious when the buffer is full. */
    ctx.fillStyle = dim; ctx.textAlign = "right"; ctx.textBaseline = "top";
    ctx.fillText(held + " pts", pad.l + pw, pad.t + 2);
  }
}

function fmt(v) {
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  return v.toFixed(3);
}

function fmtTime(s) {
  if (s >= 3600) return (s / 3600).toFixed(1) + "h";
  if (s >= 60) return (s / 60).toFixed(0) + "m";
  return s.toFixed(0) + "s";
}
