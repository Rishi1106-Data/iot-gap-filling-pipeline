"""tests/conftest.py — shared fixtures for the IoT gap-filling pipeline test suite."""

import io
import pandas as pd
import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Timeline helpers
# ---------------------------------------------------------------------------

def make_timeline(n: int = 288, freq: str = "5min", start: str = "2023-01-01") -> pd.DatetimeIndex:
    """Return a regular 5-minute DatetimeIndex of length *n*."""
    return pd.date_range(start=start, periods=n, freq=freq)


# ---------------------------------------------------------------------------
# Raw-CSV fixture (target device)
# ---------------------------------------------------------------------------

@pytest.fixture()
def raw_target_df():
    """A clean 5-min spaced raw DataFrame with TimeStamp + sensor columns."""
    tl = make_timeline(100)
    df = pd.DataFrame({
        "TimeStamp": tl.strftime("%Y-%m-%d %H:%M:%S"),
        "CurrentTemperature": np.random.default_rng(0).uniform(20, 35, 100),
        "CurrentHumidity":    np.random.default_rng(1).uniform(40, 90, 100),
        "AtmPressure":        np.random.default_rng(2).uniform(990, 1010, 100),
    })
    return df


@pytest.fixture()
def raw_target_csv(tmp_path, raw_target_df):
    """Write raw_target_df to a temp CSV and return the path."""
    p = tmp_path / "target.csv"
    raw_target_df.to_csv(p, index=False)
    return str(p)


# ---------------------------------------------------------------------------
# Fully reindexed main DataFrame (after build_timeline)
# ---------------------------------------------------------------------------

@pytest.fixture()
def main_df():
    """A 5-min timeline DataFrame with gaps in CurrentTemperature.

    Structure
    ---------
    - 288 rows (one full day)
    - 3 continuous sensor columns
    - row 10  : isolated gap (1 row)
    - rows 50-52: medium gap  (3 rows)
    - rows 100-110: long gap  (11 rows)
    """
    tl = make_timeline(288)
    rng = np.random.default_rng(42)
    df = pd.DataFrame(
        {
            "CurrentTemperature": rng.uniform(20, 35, 288),
            "CurrentHumidity":    rng.uniform(40, 90, 288),
            "AtmPressure":        rng.uniform(990, 1010, 288),
        },
        index=tl,
    )
    df.index.name = "TimeStamp"
    # Introduce gaps
    df.iloc[10, 0] = np.nan                       # isolated
    df.iloc[50:53, 0] = np.nan                    # medium (3)
    df.iloc[100:111, 0] = np.nan                  # long (11)
    return df


@pytest.fixture()
def tracked_df(main_df):
    """main_df with tracking columns already initialised (no gaps filled yet)."""
    from src.interpolation import init_tracking_columns
    from src.gap_analysis import classify_gaps

    ref_col = "CurrentTemperature"
    (gap_type_map, gap_id_map, gap_size_map,
     iso_idx, med_idx, long_idx,
     gsizes, groups, _) = classify_gaps(main_df, ref_col)

    df = init_tracking_columns(
        main_df.copy(), ref_col, gap_type_map, gap_id_map, gap_size_map
    )
    return df, iso_idx, med_idx, long_idx, gsizes, groups


# ---------------------------------------------------------------------------
# Neighbour CSV fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def neighbor_csv(tmp_path):
    """One CSV neighbour file with corrected + raw sensor columns."""
    tl = make_timeline(288)
    rng = np.random.default_rng(7)
    df = pd.DataFrame({
        "TimeStamp":          tl.strftime("%Y-%m-%d %H:%M:%S"),
        "CurrentTemperature": rng.uniform(20, 35, 288),
        "CorrectedTemp":      rng.uniform(20, 35, 288),
        "CurrentHumidity":    rng.uniform(40, 90, 288),
        "CorrectedHumidity":  rng.uniform(40, 90, 288),
        "CorrectedHeatIndex": rng.uniform(25, 40, 288),
        "AtmPressure":        rng.uniform(990, 1010, 288),
        "WindSpeed":          rng.uniform(0, 10, 288),
        "RainfallHourly":     np.zeros(288),
    })
    p = tmp_path / "neighbor.csv"
    df.to_csv(p, index=False)
    return str(p)


# ---------------------------------------------------------------------------
# Clipping / threshold configs
# ---------------------------------------------------------------------------

@pytest.fixture()
def clip():
    return {
        "CurrentTemperature": (-10.0, 60.0),
        "CurrentHumidity":    (0.0, 100.0),
        "AtmPressure":        (900.0, 1100.0),
    }


@pytest.fixture()
def jump_thresholds():
    return {
        "CurrentTemperature": 10.0,
        "CurrentHumidity":    20.0,
        "AtmPressure":        15.0,
    }


@pytest.fixture()
def cont_vars():
    return ["CurrentTemperature", "CurrentHumidity", "AtmPressure"]
