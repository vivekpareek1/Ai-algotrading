#!/bin/bash
# setup.sh — run this ONCE on your Oracle server after cloning the repo.
# It installs Python if missing, and runs a quick self-test.
set -e

echo "== Checking Python =="
if ! command -v python3 &> /dev/null; then
    apt update && apt install -y python3
fi
python3 --version

echo ""
echo "== Testing the risk module =="
python3 integration_example.py

echo ""
echo "== Done =="
echo "Next: edit config.json with your real total_capital, then re-run:"
echo "    python3 integration_example.py"
echo "to confirm it picks up your changes."
