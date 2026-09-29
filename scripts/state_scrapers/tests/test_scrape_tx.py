"""Unit tests for the Texas statutes scraper (no network)."""
from src.scrapers.us.states.tx.statutes import scrapeTX as tx


# ---------------------------------------------------------------------------
# Task 1: single-code runs via TX_ONLY_CODES
# ---------------------------------------------------------------------------

def test_work_items_defaults_to_every_code_not_done():
    items = tx._work_items(titles_done={"AG", "AL"}, only_codes="")
    codes = [c for c, _ in items]
    assert "AG" not in codes and "AL" not in codes
    assert len(codes) == len(tx.TX_CODES) - 2


def test_work_items_only_codes_filters_after_done_set():
    items = tx._work_items(titles_done={"AG"}, only_codes="pe, AG ,cv")
    assert [c for c, _ in items] == ["CV", "PE"]


def test_work_items_only_codes_unknown_code_is_ignored():
    items = tx._work_items(titles_done=set(), only_codes="ZZ,PE")
    assert [c for c, _ in items] == ["PE"]
