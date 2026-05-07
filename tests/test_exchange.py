#!/usr/bin/env python3
"""Backward-compatibility shim — delegates to run_all.py."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_all import main
if __name__ == "__main__":
    main()
