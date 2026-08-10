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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from risk_manager import RiskManager

CONFIG_PATH = Path(__file__).parent / "config.json"
PORT = 8080


def get_dashboard_password():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    return cfg.get("dashboard_password", "changeme")


def render_page(rm: RiskManager) -> str:
    status = rm.get_status()
    positions = rm.list_open_positions()

    blocked = status["blocked_for_today"]
    badge_color = "#dc2626" if blocked else "#16a34a"
    badge_text = "TRADING BLOCKED" if blocked else "TRADING ACTIVE"

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
</style>
</head>
<body>
<div class="container">
  <h1>Trading Risk Dashboard</h1>
  <div class="subtitle">Trading day: {status['trading_day']} &nbsp;|&nbsp; Auto-refreshes every 5s &nbsp;|&nbsp; Last updated: {datetime.now().strftime('%H:%M:%S')}</div>

  <span class="badge">{badge_text}</span>
  {f'<div class="reason-box">{status["block_reason"]}</div>' if blocked else ''}
  {unblock_button}

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

  <div class="footer">Risk & Position-Sizing Module &middot; refreshes automatically</div>
</div>

<script>
setTimeout(() => location.reload(), 5000);
function unblock() {{
  if (!confirm("Are you sure you want to unblock trading? Only do this if you understand why the daily loss limit fired.")) return;
  fetch('/api/unblock', {{method: 'POST'}}).then(() => location.reload());
}}
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
