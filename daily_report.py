"""
daily_report.py
================
Generates a daily trading summary (trades taken, win rate, P&L, blocks)
from trade_journal.csv and the live risk module status, and emails it.

Run manually anytime: python3 daily_report.py
Or let cron run it automatically every day after market close (see
setup instructions given alongside this script).
"""

import json
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from risk_manager import RiskManager

CREDS_PATH = Path(__file__).parent / "email_credentials.json"
JOURNAL_PATH = Path(__file__).parent / "trade_journal.csv"
CONFIG_PATH = Path(__file__).parent / "config.json"


def _get_tz():
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        return ZoneInfo(cfg.get("timezone", "Asia/Kolkata"))
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def build_report_text(days_back=1):
    """days_back=1 means 'today' (or the most recent day with data if
    the journal is empty for today, e.g. weekend)."""
    if not JOURNAL_PATH.exists():
        return "No trade_journal.csv found yet — nothing to report."

    df = pd.read_csv(JOURNAL_PATH, parse_dates=["timestamp"])
    if df.empty:
        return "Trade journal is empty — no activity yet."

    tz = _get_tz()
    # journal timestamps may or may not carry tz info depending on how they
    # were written — normalize both sides to the same tz-aware basis so the
    # comparison below never raises a tz-naive vs tz-aware error.
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(tz)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(tz)

    cutoff = datetime.now(tz) - timedelta(days=days_back)
    recent = df[df["timestamp"] >= cutoff]

    closed = recent[recent["event"] == "CLOSED"].copy()
    opened = recent[recent["event"] == "OPENED"]
    blocked = recent[recent["event"] == "BLOCKED"]
    kill_switch = recent[recent["event"] == "KILL_SWITCH"]

    lines = []
    lines.append(f"TRADING REPORT — last {days_back} day(s)")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 50)

    lines.append(f"\nTrades opened: {len(opened)}")
    lines.append(f"Trades closed: {len(closed)}")

    if not closed.empty:
        closed["pnl"] = pd.to_numeric(closed["pnl"], errors="coerce")
        wins = (closed["pnl"] > 0).sum()
        losses = (closed["pnl"] <= 0).sum()
        total_pnl = closed["pnl"].sum()
        win_rate = 100 * wins / len(closed) if len(closed) else 0
        lines.append(f"Wins / Losses: {wins} / {losses}")
        lines.append(f"Win rate: {win_rate:.1f}%")
        lines.append(f"Total P&L (closed trades): Rs.{total_pnl:,.2f}")
        if "reason" in closed.columns:
            non_empty_reasons = closed["reason"].dropna()
            non_empty_reasons = non_empty_reasons[non_empty_reasons.astype(str).str.strip() != ""]
            if not non_empty_reasons.empty:
                lines.append(f"\nExit reasons:\n{non_empty_reasons.value_counts().to_string()}")
    else:
        lines.append("No trades closed in this period.")

    if not blocked.empty:
        lines.append(f"\nSignals BLOCKED: {len(blocked)}")
        top_reasons = blocked["reason"].value_counts().head(3)
        lines.append(f"Top block reasons:\n{top_reasons.to_string()}")

    if not kill_switch.empty:
        lines.append(f"\n⚠ Daily loss kill switch fired {len(kill_switch)} time(s) in this period.")

    # live current status, regardless of the days_back window
    try:
        rm = RiskManager(str(CONFIG_PATH))
        status = rm.get_status()
        lines.append("\n" + "-" * 50)
        lines.append("CURRENT LIVE STATUS")
        lines.append("-" * 50)
        lines.append(f"Total capital: Rs.{status['total_capital']:,.0f}")
        lines.append(f"Realized P&L today: Rs.{status['realized_pnl_today']:,.2f}")
        lines.append(f"Open positions: {status['open_positions_count']}")
        lines.append(f"Committed risk: Rs.{status['committed_risk']:,.0f} "
                      f"(cap: Rs.{status['max_open_risk_amount']:,.0f})")
        lines.append(f"Trading blocked right now: {status['blocked_for_today']}")
        if status["blocked_for_today"]:
            lines.append(f"Block reason: {status['block_reason']}")
    except Exception as e:
        lines.append(f"\n(Could not fetch live status: {e})")

    return "\n".join(lines)


def send_email(subject, body):
    if not CREDS_PATH.exists():
        print(f"{CREDS_PATH} not found — run setup_email_credentials.py first.")
        return False

    creds = json.loads(CREDS_PATH.read_text())
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = creds["gmail_address"]
    msg["To"] = creds["to_email"]

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(creds["gmail_address"], creds["gmail_app_password"])
            server.send_message(msg)
        print(f"Email sent to {creds['to_email']}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("Email FAILED: authentication error. Check that you used an "
              "App Password (16 chars), not your normal Gmail password, and "
              "that 2-Step Verification is enabled on the sending account.")
        return False
    except Exception as e:
        print(f"Email FAILED: {e}")
        return False


if __name__ == "__main__":
    report = build_report_text(days_back=1)
    print(report)
    print("\nSending email...")
    today = datetime.now().strftime("%Y-%m-%d")
    send_email(f"Trading Report — {today}", report)
