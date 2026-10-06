"""pytest configuration for processor tests."""
import sys
import os

_ROOT = os.path.join(os.path.dirname(__file__), "../..")
# Allow importing `processor.src.*` from the bridge-v2 root
sys.path.insert(0, _ROOT)
# bridge_shared (shared/bridge_shared) — in the image it sits next to src/ under /app
sys.path.insert(0, os.path.join(_ROOT, "shared"))
