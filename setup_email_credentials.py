"""
setup_email_credentials.py
============================
Interactive setup for the daily email report — same pattern as
setup_credentials.py, no manual JSON editing.

You'll need a Gmail account and an "App Password" (not your normal Gmail
password — Gmail requires this for programmatic sending since 2022+).

How to get an App Password:
  1. Go to myaccount.google.com/security
  2. Make sure 2-Step Verification is turned ON (required for App Passwords)
  3. Search for "App Passwords" in the search bar at the top of that page
  4. Create one — name it "trading-server", copy the 16-character password
  5. Use THAT here, not your normal Gmail login password

Run: python3 setup_email_credentials.py
"""
import json
import getpass
from pathlib import Path

print("Setting up daily email report.")
print("(app password won't be shown as you type, that's normal)\n")

gmail_address = input("Gmail address to SEND from: ").strip()
gmail_app_password = getpass.getpass("Gmail App Password (16 characters, not your login password): ").strip()
to_email = input("Email address to RECEIVE the daily report at: ").strip()

creds = {
    "gmail_address": gmail_address,
    "gmail_app_password": gmail_app_password,
    "to_email": to_email,
}

path = Path(__file__).parent / "email_credentials.json"
path.write_text(json.dumps(creds, indent=4))
print(f"\nSaved to {path}")

check = json.loads(path.read_text())
print(f"Verified: sending from {check['gmail_address']} to {check['to_email']}")
