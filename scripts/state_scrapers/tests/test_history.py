"""Task 4: last-amended year from a Texas history line."""
from src.scrapers.us.states.tx.statutes.history import last_amended_year


def test_latest_acts_year_wins():
    line = ("Acts 1973, 63rd Leg., p. 883, ch. 399, Sec. 1, eff. Jan. 1, 1974. "
            "Amended by Acts 1993, 73rd Leg., ch. 900, Sec. 1.01, eff. Sept. 1, 1994.")
    assert last_amended_year(line) == 1993


def test_added_by_acts_line():
    line = "Added by Acts 2021, 87th Leg., R.S., Ch. 62 (S.B. 8), Sec. 5, eff. September 1, 2021."
    assert last_amended_year(line) == 2021


def test_empty_history_is_none():
    assert last_amended_year("") is None
    assert last_amended_year(None) is None


def test_effective_date_without_acts_is_none():
    assert last_amended_year("eff. Jan. 1, 2030.") is None


def test_multiline_history_takes_max_across_lines():
    line = "Acts 1985, 69th Leg., ch. 959, Sec. 1, eff. Sept. 1, 1985.\nAmended by Acts 2019, 86th Leg., R.S., Ch. 203 (H.B. 2820), Sec. 1.10(3), eff. September 1, 2019.\nAmended by Acts 2001, 77th Leg., ch. 1, eff. 2001."
    assert last_amended_year(line) == 2019
