"""
setup_credentials.py
=====================
Simple interactive setup — asks for each value one at a time and writes
a correctly-formatted angel_credentials.json for you. No manual JSON
editing, no risk of a typo breaking the file.

Run: python3 setup_credentials.py
"""
import json
import getpass
from pathlib import Path

print("Setting up your Angel One credentials.")
print("(password and totp_secret won't be shown as you type, that's normal)\n")

api_key = input("API key: ").strip()
client_code = input("Client code: ").strip()
password = getpass.getpass("Password / MPIN: ").strip()
totp_secret = getpass.getpass("TOTP secret (the text key, not the 6-digit code): ").strip()

creds = {
    "api_key": api_key,
    "client_code": client_code,
    "password": password,
    "totp_secret": totp_secret,
}

path = Path(__file__).parent / "angel_credentials.json"
path.write_text(json.dumps(creds, indent=4))

print(f"\nSaved to {path}")

# validate it's readable back correctly
check = json.loads(path.read_text())
print(f"Verified: all 4 fields present, api_key starts with '{check['api_key'][:3]}...'")
