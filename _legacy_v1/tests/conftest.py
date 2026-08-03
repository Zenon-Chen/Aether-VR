"""Shared pytest fixtures for Aether tests."""

import os
import sys
import pytest

# Ensure the project root is on sys.path so `aether` package is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Set a dummy API key so config loading doesn't raise
os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key-for-unit-tests")
