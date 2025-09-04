#!/bin/bash

# AUR Utility Cron Wrapper
# This script runs the auto-update function of aurutil.py
# Perfect for cron jobs to check and rebuild outdated packages

set -e

# Change to the script directory
cd "$(dirname "$0")"

# Run aurutil.py with no arguments to check and rebuild outdated packages
python aurutil.py
