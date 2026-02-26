"""pytest configuration for processor tests."""
import sys
import os

# Allow importing `processor.src.*` from the bridge-v2 root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
