"""tests/test_interpolation.py — full coverage of src/interpolation.py."""

import numpy as np
import pandas as pd
import pytest

from src.gap_analysis import classify_gaps
from src.interpolation import init_tracking_columns, fill_isolated_gaps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_df(n: int = 40, gap_slices=None) -> pd.DataFrame:
    tl = pd.date_range("2023-01-01", periods=n, freq="5min")
    rng = np.random.default_rng(99)
    df = pd.DataFrame(
        {
            "CurrentTemperature": rng.uniform(20, 35, n),
            "CurrentHumidity":    rng.uniform(40, 90, n),
        },
        index=tl,
    )
    df.index.name = "TimeStamp"
    if gap_slices:
        for s in gap_slices:
            df.iloc[s, 0] = np.nan
    return df


def _with_tracking(df, ref_col="CurrentTemperature"):
    (gtm, gim, gsm, iso, med, lon, gsizes, groups, _) = classify_gaps(df.copy(), ref_col)
    tracked = init_tracking_columns(df.copy(), ref_col, gtm, gim, gsm)
    return tracked, iso, med, lon, gsizes, groups


# ===========================================================================
# init_tracking_columns
# ===========================================================================

class TestInitTrackingColumns:

    def test_columns_created(self):
        df = _base_df(20)
        tracked, *_ = _with_tracking(df)
        for col in ["gap_type", "gap_id", "gap_size", "imputation_method",
                    "confidence_level", "filled_flag"]:
            assert col in tracked.columns

    def test_present_rows_tagged_original(self):
        df = _base_df(20)
        tracked, *_ = _with_tracking(df)
        non_null_mask = tracked["CurrentTemperature"].notna()
        assert (tracked.loc[non_null_mask, "imputation_method"] == "original").all()
        assert (tracked.loc[non_null_mask, "confidence_level"] == "High").all()
        assert (tracked.loc[non_null_mask, "filled_flag"] == 0).all()

    def test_missing_rows_tagged_unresolved(self):
        df = _base_df(20, [slice(5, 6)])  # 1 gap
        tracked, *_ = _with_tracking(df)
        null_mask = tracked["CurrentTemperature"].isna()
        assert (tracked.loc[null_mask, "imputation_method"] == "unresolved").all()
        assert (tracked.loc[null_mask, "confidence_level"] == "unknown").all()
        assert (tracked.loc[null_mask, "filled_flag"] == 1).all()

    def test_gap_type_no_gap_for_present_rows(self):
        df = _base_df(20, [slice(10, 11)])
        tracked, *_ = _with_tracking(df)
        # present row index 0 should be 'original' gap_type (not in gap_type_map → default)
        assert tracked.iloc[0]["gap_type"] == "original"

    def test_gap_type_isolated_assigned(self):
        df = _base_df(20, [slice(10, 11)])
        tracked, *_ = _with_tracking(df)
        assert tracked.iloc[10]["gap_type"] == "isolated"

    def test_gap_type_medium_assigned(self):
        df = _base_df(30, [slice(15, 18)])
        tracked, *_ = _with_tracking(df)
        assert tracked.iloc[15]["gap_type"] == "medium"
        assert tracked.iloc[17]["gap_type"] == "medium"

    def test_gap_id_zero_for_original_rows(self):
        df = _base_df(20)
        tracked, *_ = _with_tracking(df)
        assert (tracked["gap_id"] == 0).all()

    def test_gap_id_nonzero_for_gap_rows(self):
        df = _base_df(20, [slice(5, 6)])
        tracked, *_ = _with_tracking(df)
        assert tracked.iloc[5]["gap_id"] != 0

    def test_gap_size_for_medium_gap(self):
        df = _base_df(30, [slice(10, 13)])  # 3-row gap
        tracked, *_ = _with_tracking(df)
        for i in range(10, 13):
            assert tracked.iloc[i]["gap_size"] == 3

    def test_original_rows_have_zero_gap_size(self):
        df = _base_df(20)
        tracked, *_ = _with_tracking(df)
        assert (tracked["gap_size"] == 0).all()

    def test_no_nan_in_tracking_columns(self):
        df = _base_df(20, [slice(5, 8)])
        tracked, *_ = _with_tracking(df)
        for col in ["gap_type", "imputation_method", "confidence_level", "filled_flag"]:
            assert tracked[col].notna().all()


# ===========================================================================
# fill_isolated_gaps
# ===========================================================================

class TestFillIsolatedGaps:

    def _setup(self, n=30, iso_slices=None, extra_slices=None):
        """Return a tracked df with isolated gaps ready for filling."""
        all_slices = []
        if iso_slices:
            all_slices.extend(iso_slices)
        if extra_slices:
            all_slices.extend(extra_slices)
        df = _base_df(n, all_slices)
        tracked, iso, med, lon, gsizes, groups = _with_tracking(df)
        return tracked, iso, med, lon, gsizes, groups

    def test_isolated_gap_filled(self):
        tracked, iso, *_ = _setup = self._setup(n=30, iso_slices=[slice(10, 11)])
        cont_vars = ["CurrentTemperature"]
        clip = {"CurrentTemperature": (-10.0, 60.0), "CurrentHumidity": (0.0, 100.0)}
        result = fill_isolated_gaps(tracked, cont_vars, iso, clip, "CurrentTemperature")
        assert result.iloc[10]["CurrentTemperature"] != np.nan
        assert not pd.isna(result.iloc[10]["CurrentTemperature"])

    def test_isolated_method_tagged_interpolation(self):
        tracked, iso, *_ = self._setup(n=30, iso_slices=[slice(10, 11)])
        result = fill_isolated_gaps(
            tracked, ["CurrentTemperature"], iso,
            {"CurrentTemperature": (-10.0, 60.0)}, "CurrentTemperature"
        )
        assert result.iloc[10]["imputation_method"] == "interpolation"

    def test_isolated_confidence_tagged_high(self):
        tracked, iso, *_ = self._setup(n=30, iso_slices=[slice(10, 11)])
        result = fill_isolated_gaps(
            tracked, ["CurrentTemperature"], iso,
            {"CurrentTemperature": (-10.0, 60.0)}, "CurrentTemperature"
        )
        assert result.iloc[10]["confidence_level"] == "High"

    def test_isolated_filled_flag_set(self):
        tracked, iso, *_ = self._setup(n=30, iso_slices=[slice(10, 11)])
        result = fill_isolated_gaps(
            tracked, ["CurrentTemperature"], iso,
            {"CurrentTemperature": (-10.0, 60.0)}, "CurrentTemperature"
        )
        assert result.iloc[10]["filled_flag"] == 1

    def test_medium_gap_not_filled(self):
        # Create both an isolated and a medium gap; only isolated should be filled
        df = _base_df(50, [slice(5, 6), slice(25, 28)])  # iso at 5, medium at 25-27
        tracked, iso, med, lon, gsizes, groups = _with_tracking(df)
        result = fill_isolated_gaps(
            tracked.copy(), ["CurrentTemperature"], iso,
            {"CurrentTemperature": (-10.0, 60.0)}, "CurrentTemperature"
        )
        # Medium rows still NaN
        for i in range(25, 28):
            assert pd.isna(result.iloc[i]["CurrentTemperature"])

    def test_long_gap_not_filled(self):
        df = _base_df(60, [slice(5, 6), slice(30, 45)])  # iso + long
        tracked, iso, *_ = _with_tracking(df)
        result = fill_isolated_gaps(
            tracked.copy(), ["CurrentTemperature"], iso,
            {"CurrentTemperature": (-10.0, 60.0)}, "CurrentTemperature"
        )
        for i in range(30, 45):
            assert pd.isna(result.iloc[i]["CurrentTemperature"])

    def test_no_gaps_no_change(self):
        df = _base_df(20)
        tracked, iso, *_ = _with_tracking(df)
        assert len(iso) == 0
        before = tracked["CurrentTemperature"].copy()
        result = fill_isolated_gaps(
            tracked.copy(), ["CurrentTemperature"], iso,
            {"CurrentTemperature": (-10.0, 60.0)}, "CurrentTemperature"
        )
        pd.testing.assert_series_equal(before, result["CurrentTemperature"])

    def test_interpolated_value_between_neighbours(self):
        """Interpolated value should be between the two surrounding real values."""
        df = _base_df(30)
        # Force exact boundary values so we can check interpolation
        df.iloc[10, 0] = 20.0
        df.iloc[12, 0] = 30.0
        df.iloc[11, 0] = np.nan   # isolated gap

        tracked, iso, *_ = _with_tracking(df)
        result = fill_isolated_gaps(
            tracked.copy(), ["CurrentTemperature"], iso,
            {"CurrentTemperature": (-10.0, 60.0)}, "CurrentTemperature"
        )
        filled_val = result.iloc[11]["CurrentTemperature"]
        assert 20.0 <= filled_val <= 30.0

    def test_multiple_isolated_gaps_all_filled(self):
        df = _base_df(60, [slice(5, 6), slice(20, 21), slice(40, 41)])
        tracked, iso, *_ = _with_tracking(df)
        result = fill_isolated_gaps(
            tracked.copy(), ["CurrentTemperature"], iso,
            {"CurrentTemperature": (-10.0, 60.0)}, "CurrentTemperature"
        )
        for i in [5, 20, 40]:
            assert not pd.isna(result.iloc[i]["CurrentTemperature"])

    def test_non_ref_col_also_filled_when_listed(self):
        """If Humidity also has an isolated gap at the same position, it gets filled too."""
        df = _base_df(30)
        df.iloc[10, 0] = np.nan  # CurrentTemperature gap
        df.iloc[10, 1] = np.nan  # CurrentHumidity gap at same position
        tracked, iso, *_ = _with_tracking(df)
        result = fill_isolated_gaps(
            tracked.copy(), ["CurrentTemperature", "CurrentHumidity"], iso,
            {"CurrentTemperature": (-10.0, 60.0), "CurrentHumidity": (0.0, 100.0)},
            "CurrentTemperature"
        )
        assert not pd.isna(result.iloc[10]["CurrentHumidity"])
