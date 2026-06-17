#!/bin/bash
# Restart the scanner and tail logs. Run after editing code or config.
sudo systemctl restart sec-scanner
echo "Restarted. Tailing logs (Ctrl+C to stop)..."
journalctl -u sec-scanner -f
