#!/bin/bash
# ─────────────────────────────────────────────────────
# VESPER GMAIL EXPORTER
# Logic lives in export_gmail.py — fill credentials here.
#
# SETUP: Get App Password at https://myaccount.google.com/apppasswords
# ─────────────────────────────────────────────────────
GMAIL_ADDRESS="your_gmail@gmail.com"
GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"  # App Password from https://myaccount.google.com/apppasswords

python3 "$(dirname "$0")/export_gmail.py"
echo "Gmail synced: $(date)"
