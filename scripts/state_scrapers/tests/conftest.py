"""Make ``scripts/state_scrapers`` importable so tests can import scrapers as
``src.scrapers...`` and the pipeline as ``vaquill_pipeline...``."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
