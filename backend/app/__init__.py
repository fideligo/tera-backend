"""Tera backend — hybrid, cuff-referenced home blood-pressure monitoring.

The invariants this package exists to enforce are listed in ``CLAUDE.md`` at the repo root.
The most consequential one, worth repeating at the top of the package: **no response derived
from SCG-PPG may contain a blood-pressure value.** Only ``cuff_reading`` holds mmHg.
"""

__version__ = "0.1.0"
