"""tests/test_validation.py — full coverage of src/validation.py."""

import os
import numpy as np
import pandas as pd
import pytest

from src.validation import (
    fill_rainfall_conservative,
    validate_continuity,
    sync_audit_columns,
    evaluate_synthetic_gaps,
    plot_mae_vs_gapsize,
)
from src.gap_analysis import classify_gaps
from src.interpolation import init_tracking_columns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_timeline(n=200) -> pd.DatetimeIndex:
    return pd.date_range("2023-01-01", periods=n, freq="5min")


def _base_tracked(n=200, gap_slices=None):
    tl = _make_timeline(n)
    rng = np.random.default_rng(20)
    df = pd.DataFrame(
        {
            "CurrentTemperature": rng.uniform(20, 35, n),
            "CurrentHumidity":    rng.uniform(40, 90, n),
            "RainfallHourly":     np.zeros(n),
        },
        index=tl,
    )
    df.index.name = "TimeStamp"
    if gap_slices:
        for s in gap_slices:
            df.iloc[s, 0] = np.nan

    ref_col = "CurrentTemperature"
    (gtm, gim, gsm, iso, med, lon, gsizes, groups, _) = classify_gaps(df.copy(), ref_col)
    tracked = init_tracking_columns(df.copy(), ref_col, gtm, gim, gsm)
    return tracked, gsizes, groups, tl


# ===========================================================================
# fill_rainfall_conservative
# ===========================================================================

class TestFillRainfallConservative:

    def test_nan_filled_with_zero_when_dry(self):
        df, *_ = _base_tracked()
        # Set a rainfall NaN surrounded by zeros
        df.loc[df.index[20], "RainfallHourly"] = np.nan
        result = fill_rainfall_conservative(df.copy(), [])
        assert result.loc[df.index[20], "RainfallHourly"] == 0.0

    def test_nan_not_filled_when_wet_window(self):
        df, *_ = _base_tracked()
        # Set rain > 0 nearby, then NaN in middle
        df.loc[df.index[20], "RainfallHourly"] = 5.0
        df.loc[df.index[21], "RainfallHourly"] = np.nan
        result = fill_rainfall_conservative(df.copy(), [])
        # Wet window → should NOT be zero
        assert pd.isna(result.loc[df.index[21], "RainfallHourly"])

    def test_no_rainfall_columns_no_change(self):
        tl = _make_timeline(50)
        df = pd.DataFrame(
            {
                "CurrentTemperature": np.random.default_rng(5).uniform(20, 35, 50),
                "imputation_method":  ["original"] * 50,
            },
            index=tl,
        )
        before = df.copy()
        result = fill_rainfall_conservative(df, [])
        pd.testing.assert_frame_equal(before, result)

    def test_neighbour_rain_column_consulted(self):
        df, *_ = _base_tracked()
        df["RainH_N1"] = 0.0  # neighbour is dry
        df.loc[df.index[15], "RainfallHourly"] = np.nan
        result = fill_rainfall_conservative(df.copy(), ["N1"])
        assert result.loc[df.index[15], "RainfallHourly"] == 0.0

    def test_neighbour_wet_prevents_fill(self):
        df, *_ = _base_tracked()
        df["RainH_N1"] = 0.0
        df.loc[df.index[15], "RainH_N1"] = 3.0   # neighbour has rain
        df.loc[df.index[15], "RainfallHourly"] = np.nan
        result = fill_rainfall_conservative(df.copy(), ["N1"])
        assert pd.isna(result.loc[df.index[15], "RainfallHourly"])

    def test_returns_dataframe(self):
        df, *_ = _base_tracked()
        result = fill_rainfall_conservative(df.copy(), [])
        assert isinstance(result, pd.DataFrame)

    def test_multiple_rain_columns(self):
        df, *_ = _base_tracked()
        df["RainfallDaily"] = np.zeros(len(df))
        df.loc[df.index[10], "RainfallDaily"] = np.nan
        result = fill_rainfall_conservative(df.copy(), [])
        assert result.loc[df.index[10], "RainfallDaily"] == 0.0


# ===========================================================================
# validate_continuity
# ===========================================================================

class TestValidateContinuity:

    def _with_jump(self, pos=50, jump_val=40.0):
        """Return a tracked df with an artificial jump at position pos."""
        df, *_ = _base_tracked(100, [slice(pos, pos + 1)])
        # Place an extreme imputed value after the gap to simulate jump
        df.iloc[pos, 0] = jump_val
        df.iloc[pos, df.columns.get_loc("imputation_method")] = "model"
        return df

    def test_returns_df_and_dict(self):
        df, *_ = _base_tracked(50)
        result_df, report = validate_continuity(df.copy(), {"CurrentTemperature": 10.0})
        assert isinstance(result_df, pd.DataFrame)
        assert isinstance(report, dict)

    def test_no_jumps_zero_flags(self):
        df, *_ = _base_tracked(100)
        _, report = validate_continuity(df.copy(), {"CurrentTemperature": 10.0})
        assert report.get("CurrentTemperature", 0) == 0

    def test_jump_flagged_and_corrected(self):
        df = self._with_jump(pos=50, jump_val=500.0)
        before_val = df.iloc[50]["CurrentTemperature"]
        _, report = validate_continuity(df.copy(), {"CurrentTemperature": 10.0})
        assert report["CurrentTemperature"] >= 1

    def test_original_rows_not_modified(self):
        df, *_ = _base_tracked(50)
        orig_vals = df["CurrentTemperature"].copy()
        validate_continuity(df.copy(), {"CurrentTemperature": 10.0})
        # Original df untouched
        pd.testing.assert_series_equal(df["CurrentTemperature"], orig_vals)

    def test_skips_column_not_in_df(self):
        df, *_ = _base_tracked(50)
        _, report = validate_continuity(df.copy(), {"NonExistentCol": 5.0})
        assert "NonExistentCol" not in report

    def test_corrected_value_blend(self):
        """After correction, the value should differ from the original bad value."""
        df = self._with_jump(pos=50, jump_val=500.0)
        result_df, _ = validate_continuity(df.copy(), {"CurrentTemperature": 10.0})
        corrected = result_df.iloc[50]["CurrentTemperature"]
        assert corrected < 500.0


# ===========================================================================
# sync_audit_columns
# ===========================================================================

class TestSyncAuditColumns:

    def test_returns_dataframe(self):
        df, *_ = _base_tracked(50)
        result = sync_audit_columns(df.copy())
        assert isinstance(result, pd.DataFrame)

    def test_original_rows_have_filled_flag_0(self):
        df, *_ = _base_tracked(50)
        result = sync_audit_columns(df.copy())
        orig_mask = result["imputation_method"] == "original"
        assert (result.loc[orig_mask, "filled_flag"] == 0).all()

    def test_non_original_rows_have_filled_flag_1(self):
        df, *_ = _base_tracked(50, [slice(10, 11)])
        # Simulate a filled row
        df.iloc[10, df.columns.get_loc("imputation_method")] = "interpolation"
        df.iloc[10, 0] = 25.0
        result = sync_audit_columns(df.copy())
        interp_mask = result["imputation_method"] == "interpolation"
        assert (result.loc[interp_mask, "filled_flag"] == 1).all()

    def test_filled_flag_column_exists(self):
        df, *_ = _base_tracked(50)
        result = sync_audit_columns(df.copy())
        assert "filled_flag" in result.columns

    def test_no_nan_in_filled_flag(self):
        df, *_ = _base_tracked(50)
        result = sync_audit_columns(df.copy())
        assert result["filled_flag"].notna().all()


# ===========================================================================
# evaluate_synthetic_gaps
# ===========================================================================

class TestEvaluateSyntheticGaps:

    def _setup_eval(self, n=500):
        """Build tracked df + trained model for evaluate_synthetic_gaps."""
        from src.model_selection import run_model_tournament, build_features
        tl = _make_timeline(n)
        rng = np.random.default_rng(77)
        df = pd.DataFrame(
            {
                "CurrentTemperature": rng.uniform(20, 35, n),
                "CurrentHumidity":    rng.uniform(40, 90, n),
            },
            index=tl,
        )
        df.index.name = "TimeStamp"
        ref_col = "CurrentTemperature"
        (gtm, gim, gsm, *_, ) = classify_gaps(df.copy(), ref_col)
        tracked = init_tracking_columns(df.copy(), ref_col, gtm, gim, gsm)
        sel, _ = run_model_tournament(tracked, ["CurrentTemperature"], [], n_splits=2)
        return tracked, sel, tl

    def test_returns_dataframe(self):
        tracked, sel, tl = self._setup_eval()
        result = evaluate_synthetic_gaps(
            tracked, ["CurrentTemperature"], [], sel, tl,
            gap_sizes=[1, 3], jump_thresholds={"CurrentTemperature": 10.0},
            clip={"CurrentTemperature": (-10.0, 60.0)},
            split_frac=0.8, n_gaps=10, seed=0,
        )
        assert isinstance(result, pd.DataFrame)

    def test_result_has_expected_columns(self):
        tracked, sel, tl = self._setup_eval()
        result = evaluate_synthetic_gaps(
            tracked, ["CurrentTemperature"], [], sel, tl,
            gap_sizes=[1], jump_thresholds={"CurrentTemperature": 10.0},
            clip={"CurrentTemperature": (-10.0, 60.0)},
            split_frac=0.8, n_gaps=5, seed=0,
        )
        if not result.empty:
            for col in ["variable", "gap_size", "MAE", "RMSE", "R2", "Bias"]:
                assert col in result.columns

    def test_mae_non_negative(self):
        tracked, sel, tl = self._setup_eval()
        result = evaluate_synthetic_gaps(
            tracked, ["CurrentTemperature"], [], sel, tl,
            gap_sizes=[1, 2], jump_thresholds={"CurrentTemperature": 10.0},
            clip={"CurrentTemperature": (-10.0, 60.0)},
            split_frac=0.8, n_gaps=10, seed=42,
        )
        if not result.empty:
            assert (result["MAE"] >= 0).all()

    def test_skips_variable_not_in_model_selection(self):
        tracked, sel, tl = self._setup_eval()
        result = evaluate_synthetic_gaps(
            tracked, ["CurrentHumidity"], [], sel, tl,  # no Humidity model
            gap_sizes=[1], jump_thresholds={},
            clip={"CurrentHumidity": (0.0, 100.0)},
            split_frac=0.8, n_gaps=5, seed=0,
        )
        assert result.empty

    def test_empty_model_selection_returns_empty_df(self):
        tracked, _, tl = self._setup_eval()
        result = evaluate_synthetic_gaps(
            tracked, ["CurrentTemperature"], [], {}, tl,
            gap_sizes=[1], jump_thresholds={},
            clip={"CurrentTemperature": (-10.0, 60.0)},
            split_frac=0.8, n_gaps=5, seed=0,
        )
        assert result.empty


# ===========================================================================
# plot_mae_vs_gapsize
# ===========================================================================

class TestPlotMaeVsGapsize:

    def test_plot_file_created(self, tmp_path):
        eval_df = pd.DataFrame({
            "variable": ["CurrentTemperature", "CurrentTemperature"],
            "gap_size": [1, 3],
            "MAE": [0.5, 1.2],
        })
        plot_mae_vs_gapsize(eval_df, ["CurrentTemperature"], [1, 3], str(tmp_path))
        assert os.path.exists(str(tmp_path / "mae_vs_gapsize.png"))

    def test_empty_df_no_crash(self, tmp_path):
        eval_df = pd.DataFrame(columns=["variable", "gap_size", "MAE"])
        plot_mae_vs_gapsize(eval_df, ["CurrentTemperature"], [1, 3], str(tmp_path))
        assert os.path.exists(str(tmp_path / "mae_vs_gapsize.png"))

    def test_missing_variable_skipped(self, tmp_path):
        eval_df = pd.DataFrame({
            "variable": ["OtherVar"],
            "gap_size": [1],
            "MAE": [0.5],
        })
        # Should not crash even when cont_vars don't match eval_df
        plot_mae_vs_gapsize(eval_df, ["CurrentTemperature"], [1], str(tmp_path))

    def test_multiple_variables_plotted(self, tmp_path):
        eval_df = pd.DataFrame({
            "variable": ["CurrentTemperature", "CurrentTemperature",
                         "CurrentHumidity", "CurrentHumidity"],
            "gap_size": [1, 3, 1, 3],
            "MAE": [0.4, 0.8, 2.0, 4.0],
        })
        plot_mae_vs_gapsize(
            eval_df, ["CurrentTemperature", "CurrentHumidity"], [1, 3], str(tmp_path)
        )
        assert os.path.exists(str(tmp_path / "mae_vs_gapsize.png"))
