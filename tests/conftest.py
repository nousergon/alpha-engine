"""Add project root to sys.path so executor.* imports work."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
