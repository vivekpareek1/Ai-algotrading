"""Quick diagnostic — checks your TOTP secret and server clock without
printing any sensitive values."""
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pyotp

creds = json.loads(Path("angel_credentials.json").read_text())

print("Server UTC time:", datetime.now(timezone.utc))
print("Server local time:", datetime.now())
ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
print("Correct IST should be:", ist)

secret = creds.get("totp_secret", "")
print(f"\ntotp_secret length: {len(secret)} characters")
print(f"totp_secret looks like base32 (only A-Z, 2-7): "
      f"{all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567' for c in secret.upper())}")

try:
    totp = pyotp.TOTP(secret)
    code = totp.now()
    print(f"\nGenerated TOTP code: {code}")
    print("(Compare this to what an authenticator app shows RIGHT NOW using")
    print(" the same secret, if you have one set up. They should match.)")
except Exception as e:
    print(f"\nERROR generating TOTP — the secret is likely invalid: {e}")
