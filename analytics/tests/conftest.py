"""pytest configuration for analytics tests."""
import os
import sys

# Allow importing `flows.*` the way the container does (WORKDIR /app)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
