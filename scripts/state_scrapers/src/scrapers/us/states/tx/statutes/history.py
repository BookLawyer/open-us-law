"""Texas history-line helpers.

A Texas section's history line lists every session law that touched it:
``Acts 1973, 63rd Leg., ...`` (enactment), ``Amended by Acts 1993, ...``,
``Added by Acts 2021, ...``. The latest ``Acts YYYY`` year is the session
that last amended the section.
"""
from __future__ import annotations

import re
from typing import Optional

_ACTS_YEAR = re.compile(r"\bActs (\d{4})\b")


def last_amended_year(history: Optional[str]) -> Optional[int]:
    """Latest ``Acts YYYY`` year in ``history``, or None when there is none."""
    if not history:
        return None
    years = [int(y) for y in _ACTS_YEAR.findall(history)]
    return max(years) if years else None
