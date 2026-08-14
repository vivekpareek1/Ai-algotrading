"""
dashboard.py
============
Live web dashboard for the risk module. Shows capital, today's P&L, open
positions, and the kill-switch status — auto-refreshing, in your browser.

No external libraries needed (uses Python's built-in http.server), so
there's nothing extra to install on the server.

RUN IT:
    cd /opt/trading/risk
    python3 dashboard.py

Then open in your browser:
    http://<your-server-public-ip>:8080

Protected by a password (Basic Auth) — set/change it in config.json under
"dashboard_password". Default is "changeme" — CHANGE THIS before exposing
the port to the internet.

IMPORTANT: your server's firewall (AWS "Security Group") must allow inbound
traffic on port 8080, or the browser won't be able to reach it. See
README.md for the exact steps to open that port.
"""

import json
import base64
import csv
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from risk_manager import RiskManager

CONFIG_PATH = Path(__file__).parent / "config.json"
JOURNAL_PATH = Path(__file__).parent / "trade_journal.csv"
PORT = 8080


def get_dashboard_password():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    return cfg.get("dashboard_password", "changeme")


def get_equity_curve(limit=200):
    """
    Reads CLOSED trades from trade_journal.csv and returns a cumulative P&L
    series for charting: [{"time": "...", "pnl": float, "cumulative": float}].

    Uses only the stdlib csv module — the dashboard process has no pandas
    dependency, and there's no reason to add one just for this.

    limit: keep only the most recent N closed trades, so the chart stays
    readable and the page doesn't balloon once the journal has thousands
    of rows.
    """
    if not JOURNAL_PATH.exists():
        return []

    closed = []
    with open(JOURNAL_PATH, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("event") != "CLOSED":
                continue
            pnl_raw = row.get("pnl", "")
            try:
                pnl = float(pnl_raw) if pnl_raw not in ("", None) else None
            except ValueError:
                pnl = None
            if pnl is None:
                continue  # a CLOSED row with no parseable pnl is unusable for a curve
            closed.append({"time": row.get("timestamp", ""), "pnl": pnl})

    closed.sort(key=lambda r: r["time"])
    closed = closed[-limit:]

    running = 0.0
    curve = []
    for row in closed:
        running += row["pnl"]
        curve.append({"time": row["time"], "pnl": round(row["pnl"], 2),
                      "cumulative": round(running, 2)})
    return curve


def render_page(rm: RiskManager) -> str:
    status = rm.get_status()
    positions = rm.list_open_positions()
    journal = rm.list_recent_journal(limit=100)

    engine_on = status["engine_active"]
    kill_blocked = status["blocked_for_today"]

    if not engine_on:
        badge_color, badge_text = "#64748b", "ENGINE OFF"
    elif kill_blocked:
        badge_color, badge_text = "#dc2626", "TRADING BLOCKED (daily loss limit)"
    else:
        badge_color, badge_text = "#16a34a", "ENGINE ON — TRADING ACTIVE"

    blocked = kill_blocked  # kept for the existing unblock-button logic below

    pnl = status["realized_pnl_today"]
    pnl_color = "#16a34a" if pnl >= 0 else "#dc2626"

    rows = ""
    if positions:
        for tid, p in positions.items():
            rows += f"""
            <tr>
                <td>{tid}</td>
                <td>{p['strategy']}</td>
                <td>{p['symbol']}</td>
                <td>{p['direction']}</td>
                <td>{p['quantity']}</td>
                <td>Rs.{p['entry_price']:,.2f}</td>
                <td>Rs.{p['stop_loss_price']:,.2f}</td>
                <td>Rs.{p['risk_amount']:,.0f}</td>
            </tr>"""
    else:
        rows = '<tr><td colspan="8" style="text-align:center;color:#888;">No open positions</td></tr>'

    strat_rows = ""
    for name, used in status["capital_by_strategy"].items():
        strat_rows += f"<tr><td>{name}</td><td>Rs.{used:,.0f}</td></tr>"

    unblock_button = ""
    if blocked:
        unblock_button = """
        <button onclick="unblock()" style="background:#dc2626;color:white;border:none;
        padding:10px 20px;border-radius:6px;cursor:pointer;font-size:14px;margin-top:10px;">
            Manually Unblock Trading
        </button>
        <p style="color:#888;font-size:12px;">Only override this if you understand why the
        limit fired. It exists to protect you.</p>
        """

    if engine_on:
        engine_toggle = """
        <button onclick="toggleEngine('off')" style="background:#dc2626;color:white;border:none;
        padding:12px 24px;border-radius:6px;cursor:pointer;font-size:15px;font-weight:600;">
            Turn Engine OFF
        </button>
        """
    else:
        engine_toggle = """
        <button onclick="toggleEngine('on')" style="background:#16a34a;color:white;border:none;
        padding:12px 24px;border-radius:6px;cursor:pointer;font-size:15px;font-weight:600;">
            Turn Engine ON
        </button>
        <p style="color:#888;font-size:12px;margin-top:8px;">No trades are approved while the
        engine is off, no matter what your strategies signal.</p>
        """

    EVENT_COLORS = {
        "APPROVED": "#16a34a", "OPENED": "#16a34a", "CLOSED": "#3b82f6",
        "BLOCKED": "#dc2626", "KILL_SWITCH": "#dc2626",
        "RESERVATION_EXPIRED": "#f59e0b", "RESERVATION_CANCELLED": "#f59e0b",
        "MANUAL_UNBLOCK": "#a855f7", "ENGINE_ACTIVATED": "#16a34a",
        "ENGINE_DEACTIVATED": "#64748b",
    }
    journal_rows = ""
    if journal:
        for row in journal:
            ev = row.get("event", "")
            color = EVENT_COLORS.get(ev, "#94a3b8")
            ts = row.get("timestamp", "")[:19].replace("T", " ")
            row_pnl = row.get("pnl", "")   # NOTE: must not be named `pnl` — that
            pnl_html = ""                  # name is also used below for the
            if row_pnl:                    # card's numeric P&L; Python has no
                try:                        # block scope, so reusing it here
                    pnl_f = float(row_pnl)  # silently corrupted the card value
                    pnl_html = f'<span style="color:{"#16a34a" if pnl_f>=0 else "#dc2626"}">Rs.{pnl_f:,.0f}</span>'
                except ValueError:
                    pass
            journal_rows += f"""
            <tr>
                <td>{ts}</td>
                <td><span style="color:{color};font-weight:600;">{ev}</span></td>
                <td>{row.get('strategy','')}</td>
                <td>{row.get('symbol','')}</td>
                <td>{row.get('direction','')}</td>
                <td>{row.get('quantity','')}</td>
                <td>{pnl_html}</td>
                <td style="color:#94a3b8;font-size:12px;max-width:280px;">{row.get('reason','')}</td>
            </tr>"""
    else:
        journal_rows = '<tr><td colspan="8" style="text-align:center;color:#888;">No activity yet</td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Trading Risk Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; background: #0f172a;
         color: #e2e8f0; margin: 0; padding: 20px; }}
  .container {{ max-width: 1000px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .subtitle {{ color: #94a3b8; font-size: 13px; margin-bottom: 20px; }}
  .badge {{ display: inline-block; padding: 6px 14px; border-radius: 20px;
           font-weight: 600; font-size: 13px; color: white; background: {badge_color}; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
          gap: 14px; margin: 20px 0; }}
  .card {{ background: #1e293b; border-radius: 10px; padding: 16px; border: 1px solid #334155; }}
  .card .label {{ color: #94a3b8; font-size: 12px; text-transform: uppercase;
                 letter-spacing: 0.5px; }}
  .card .value {{ font-size: 22px; font-weight: 700; margin-top: 6px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }}
  th {{ text-align: left; color: #94a3b8; font-weight: 600; padding: 8px;
       border-bottom: 1px solid #334155; font-size: 11px; text-transform: uppercase; }}
  td {{ padding: 8px; border-bottom: 1px solid #1e293b; }}
  section {{ margin-top: 28px; }}
  section h2 {{ font-size: 15px; color: #cbd5e1; margin-bottom: 10px; }}
  .reason-box {{ background: #1e293b; border-left: 3px solid {badge_color}; padding: 12px 16px;
                border-radius: 4px; font-size: 13px; color: #cbd5e1; margin-top: 10px; }}
  .footer {{ color: #64748b; font-size: 11px; margin-top: 30px; text-align: center; }}
  #chartWrap {{ background: #1e293b; border-radius: 10px; padding: 16px;
               border: 1px solid #334155; }}
  #equityChart {{ width: 100%; height: 220px; display: block; }}
</style>
</head>
<body>
<div class="container">
  <h1>Trading Risk Dashboard</h1>
  <div class="subtitle">Trading day: {status['trading_day']} &nbsp;|&nbsp; Auto-refreshes every 5s &nbsp;|&nbsp; Last updated: {datetime.now().strftime('%H:%M:%S')}</div>

  <span class="badge">{badge_text}</span>
  {f'<div class="reason-box">{status["block_reason"]}</div>' if blocked else ''}
  {unblock_button}

  <div style="margin-top:18px;">
    {engine_toggle}
  </div>

  <div class="grid">
    <div class="card"><div class="label">Total Capital</div><div class="value">Rs.{status['total_capital']:,.0f}</div></div>
    <div class="card"><div class="label">Realized P&amp;L Today</div><div class="value" style="color:{pnl_color}">Rs.{pnl:,.0f}</div></div>
    <div class="card"><div class="label">Unrealized P&amp;L</div><div class="value">Rs.{status['unrealized_pnl']:,.0f}</div></div>
    <div class="card"><div class="label">Open Positions</div><div class="value">{status['open_positions_count']}</div></div>
    <div class="card"><div class="label">Committed Risk</div><div class="value">Rs.{status['committed_risk']:,.0f}</div></div>
    <div class="card"><div class="label">Max Open Risk</div><div class="value">Rs.{status['max_open_risk_amount']:,.0f}</div></div>
    <div class="card"><div class="label">Max Daily Loss</div><div class="value">Rs.{status['max_daily_loss_amount']:,.0f}</div></div>
    <div class="card"><div class="label">Capital Deployed</div><div class="value">Rs.{status['capital_deployed']:,.0f}</div></div>
  </div>

  <section>
    <h2>Equity Curve — cumulative P&amp;L from closed trades</h2>
    <div id="chartWrap">
      <canvas id="equityChart" width="960" height="220"></canvas>
      <div id="chartEmpty" style="display:none;color:#888;text-align:center;padding:40px 0;">
        No closed trades yet — the chart fills in as trades close.
      </div>
    </div>
  </section>

  <section>
    <h2>Open Positions</h2>
    <table>
      <tr><th>Trade ID</th><th>Strategy</th><th>Symbol</th><th>Dir</th><th>Qty</th>
          <th>Entry</th><th>Stop Loss</th><th>Risk</th></tr>
      {rows}
    </table>
  </section>

  <section>
    <h2>Capital Used by Strategy</h2>
    <table>
      <tr><th>Strategy</th><th>Capital Deployed</th></tr>
      {strat_rows}
    </table>
  </section>

  <section>
    <h2>Trade History — every decision, taken or not (most recent first)</h2>
    <table>
      <tr><th>Time</th><th>Event</th><th>Strategy</th><th>Symbol</th><th>Dir</th>
          <th>Qty</th><th>P&amp;L</th><th>Reason / Note</th></tr>
      {journal_rows}
    </table>
  </section>

  <div class="footer">Risk & Position-Sizing Module &middot; refreshes automatically</div>
</div>

<script>
setTimeout(() => location.reload(), 5000);
function unblock() {{
  if (!confirm("Are you sure you want to unblock trading? Only do this if you understand why the daily loss limit fired.")) return;
  fetch('/api/unblock', {{method: 'POST'}}).then(() => location.reload());
}}
function toggleEngine(state) {{
  const msg = state === 'on'
    ? "Turn the engine ON? Strategies will be able to place real trades from now on."
    : "Turn the engine OFF? No new trades will be approved until you turn it back on.";
  if (!confirm(msg)) return;
  fetch('/api/engine/' + state, {{method: 'POST'}}).then(() => location.reload());
}}

function drawEquityChart(data) {{
  const canvas = document.getElementById('equityChart');
  const emptyMsg = document.getElementById('chartEmpty');

  if (!data || data.length === 0) {{
    canvas.style.display = 'none';
    emptyMsg.style.display = 'block';
    return;
  }}
  canvas.style.display = 'block';
  emptyMsg.style.display = 'none';

  // render at device pixel ratio for a crisp line on retina screens, while
  // the CSS width/height (set in the stylesheet) controls the layout size
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 960;
  const cssHeight = 220;
  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const padL = 60, padR = 16, padT = 16, padB = 28;
  const plotW = cssWidth - padL - padR;
  const plotH = cssHeight - padT - padB;

  const values = data.map(d => d.cumulative);
  let min = Math.min(0, ...values);
  let max = Math.max(0, ...values);
  if (min === max) {{ min -= 1; max += 1; }}  // avoid a zero-height plot for a flat/single-point series
  const pad = (max - min) * 0.08;
  min -= pad; max += pad;

  const xFor = i => padL + (data.length === 1 ? plotW / 2 : (i / (data.length - 1)) * plotW);
  const yFor = v => padT + plotH - ((v - min) / (max - min)) * plotH;

  // zero baseline, only drawn if zero actually falls inside the visible range
  if (min < 0 && max > 0) {{
    const y0 = yFor(0);
    ctx.strokeStyle = '#475569';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padL, y0);
    ctx.lineTo(padL + plotW, y0);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#64748b';
    ctx.font = '11px monospace';
    ctx.fillText('0', 4, y0 + 4);
  }}

  // y-axis min/max labels
  ctx.fillStyle = '#64748b';
  ctx.font = '11px monospace';
  ctx.fillText('Rs.' + Math.round(max).toLocaleString(), 4, padT + 8);
  ctx.fillText('Rs.' + Math.round(min).toLocaleString(), 4, padT + plotH);

  // the line itself — green if the series ends at or above zero, red otherwise
  const finalValue = values[values.length - 1];
  ctx.strokeStyle = finalValue >= 0 ? '#16a34a' : '#dc2626';
  ctx.lineWidth = 2;
  ctx.beginPath();
  data.forEach((d, i) => {{
    const x = xFor(i), y = yFor(d.cumulative);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }});
  ctx.stroke();

  // a dot on the most recent point, with its value labelled
  const lastX = xFor(data.length - 1), lastY = yFor(finalValue);
  ctx.fillStyle = ctx.strokeStyle;
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3.5, 0, Math.PI * 2);
  ctx.fill();
  ctx.font = '12px monospace';
  ctx.textAlign = 'right';
  ctx.fillText('Rs.' + finalValue.toLocaleString(), lastX - 8, lastY - 8);
  ctx.textAlign = 'left';
}}

fetch('/api/equity-curve')
  .then(r => r.json())
  .then(drawEquityChart)
  .catch(() => {{
    document.getElementById('equityChart').style.display = 'none';
    document.getElementById('chartEmpty').style.display = 'block';
    document.getElementById('chartEmpty').textContent = 'Could not load chart data.';
  }});
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _check_auth(self):
        expected = get_dashboard_password()
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            _, _, password = decoded.partition(":")
            return password == expected
        except Exception:
            return False

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Trading Dashboard"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authentication required")

    def do_GET(self):
        if not self._check_auth():
            self._require_auth()
            return
        try:
            rm = RiskManager(str(CONFIG_PATH))
            if self.path == "/api/status":
                body = json.dumps(rm.get_status(), indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/equity-curve":
                body = json.dumps(get_equity_curve(), indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                body = render_page(rm).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Error: {e}".encode())

    def do_POST(self):
        if not self._check_auth():
            self._require_auth()
            return
        if self.path == "/api/unblock":
            rm = RiskManager(str(CONFIG_PATH))
            rm.force_unblock(reason="manual override via dashboard")
            self.send_response(200)
            self.end_headers()
        elif self.path == "/api/engine/on":
            rm = RiskManager(str(CONFIG_PATH))
            rm.activate_engine(reason="turned on via dashboard")
            self.send_response(200)
            self.end_headers()
        elif self.path == "/api/engine/off":
            rm = RiskManager(str(CONFIG_PATH))
            rm.deactivate_engine(reason="turned off via dashboard")
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep the terminal quiet; visit /api/status for raw JSON if needed


if __name__ == "__main__":
    pw = get_dashboard_password()
    if pw == "changeme":
        print("WARNING: dashboard_password is still the default 'changeme'.")
        print("Edit config.json and set a real password before exposing this port.")
    print(f"Dashboard running on http://0.0.0.0:{PORT}")
    print(f"Open from your browser: http://<your-server-ip>:{PORT}")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
