#!/bin/bash
echo "Setting up KLVR Emulator..."

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python run.py
