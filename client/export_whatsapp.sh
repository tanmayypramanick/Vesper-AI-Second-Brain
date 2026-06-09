#!/bin/bash
# VESPER WHATSAPP EXPORTER — thin wrapper, logic is in export_whatsapp.py
python3 "$(dirname "$0")/export_whatsapp.py"
echo "WhatsApp export done: $(date)"
