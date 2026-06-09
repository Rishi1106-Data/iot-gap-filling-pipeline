"""tests/test_main.py — tests for src/main.py helper functions.

run_pipeline depends on external files and trained models (an integration test);
we cover the three pure helper functions that can be unit-tested in isolation.
"""

import os
import numpy as np
import pandas as pd
import pytest

from src.main import save_outputs, _build_audit_df, _finalize_df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_timeline(n=50) -> pd.DatetimeIndex:
    return pd.date_range("2023-01-01", periods=n, freq="5min")


def _make_filled_df(n=50, neighbor_ids=None):
    """Minimal filled DataFrame with tracking + neighbour columns."""
    tl = _make_timeline(n)
    rng = np.random.default_rng(31)
    cols = {
        "CurrentTemperature": rng.uniform(20, 35, n),
        "CurrentHumidity":    rng.uniform(40, 90, n),
        "gap_type":           ["original"] * n,
        "gap_id":             [0] * n,
        "gap_size":           [0] * n,
        "filled_flag":        [0] * n,
        "imputation_method":  ["original"] * n,
        "confidence_level":   ["High"] * n,
    }
    if neighbor_ids:
        for nid in neighbor_ids:
            cols[f"Temp_{nid}"]     = rng.uniform(20, 35, n)
            cols[f"CorrTemp_{nid}"] = rng.uniform(20, 35, n)
    df = pd.DataFrame(cols, index=tl)
    df.index.name = "TimeStamp"
    return df


def _make_output_paths(tmp_path):
    return {
        "output_dir":              str(tmp_path),
        "filled_dataset":          str(tmp_path / "filled.csv"),
        "audit_report":            str(tmp_path / "audit.csv"),
        "evaluation_report":       str(tmp_path / "evaluation.csv"),
        "neighbor_quality_report": str(tmp_path / "neighbor_quality.csv"),
        "model_selection_report":  str(tmp_path / "model_selection.csv"),
    }


# ===========================================================================
# save_outputs
# ===========================================================================

class TestSaveOutputs:

    def test_all_files_created(self, tmp_path):
        paths = _make_output_paths(tmp_path)
        final_df   = _make_filled_df()
        audit_df   = pd.DataFrame({"variable": ["Temp"], "original": [40]})
        eval_df    = pd.DataFrame({"variable": ["Temp"], "MAE": [0.5]})
        nbr_q_df   = pd.DataFrame({"neighbor": ["N1"], "coverage_pct": [95.0]})
        ms_df      = pd.DataFrame({"variable": ["Temp"], "best_model": ["KNN"]})

        save_outputs(final_df, audit_df, eval_df, nbr_q_df, ms_df, paths)

        for key in ["filled_dataset", "audit_report", "evaluation_report",
                    "neighbor_quality_report", "model_selection_report"]:
            assert os.path.exists(paths[key]), f"Missing: {key}"

    def test_output_dir_created_if_missing(self, tmp_path):
        new_dir = tmp_path / "new_subdir"
        paths = {
            "output_dir":              str(new_dir),
            "filled_dataset":          str(new_dir / "filled.csv"),
            "audit_report":            str(new_dir / "audit.csv"),
            "evaluation_report":       str(new_dir / "eval.csv"),
            "neighbor_quality_report": str(new_dir / "nq.csv"),
            "model_selection_report":  str(new_dir / "ms.csv"),
        }
        save_outputs(
            _make_filled_df(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            paths,
        )
        assert os.path.isdir(str(new_dir))

    def test_model_selection_df_none_skipped(self, tmp_path):
        paths = _make_output_paths(tmp_path)
        save_outputs(
            _make_filled_df(),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.DataFrame(),
            None,   # model_selection_df = None
            paths,
        )
        # model_selection_report should not be created
        assert not os.path.exists(paths["model_selection_report"])

    def test_filled_dataset_has_timestamp_column(self, tmp_path):
        paths = _make_output_paths(tmp_path)
        save_outputs(
            _make_filled_df(), pd.DataFrame(), pd.DataFrame(),
            pd.DataFrame(), None, paths
        )
        loaded = pd.read_csv(paths["filled_dataset"])
        assert "TimeStamp" in loaded.columns

    def test_filled_dataset_row_count(self, tmp_path):
        paths = _make_output_paths(tmp_path)
        df = _make_filled_df(n=30)
        save_outputs(df, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), None, paths)
        loaded = pd.read_csv(paths["filled_dataset"])
        assert len(loaded) == 30


# ===========================================================================
# _build_audit_df
# ===========================================================================

class TestBuildAuditDf:

    def _make_df_with_methods(self, n=40):
        tl = pd.date_range("2023-01-01", periods=n, freq="5min")
        df = pd.DataFrame(
            {
                "CurrentTemperature": np.random.default_rng(0).uniform(20, 35, n),
                "CurrentHumidity":    np.random.default_rng(1).uniform(40, 90, n),
                "imputation_method": (
                    ["original"] * 30
                    + ["interpolation"] * 5
                    + ["model"] * 3
                    + ["neighbor"] * 2
                ),
            },
            index=tl,
        )
        return df

    def test_returns_dataframe(self):
        df = self._make_df_with_methods()
        result = _build_audit_df(df, ["CurrentTemperature"], [])
        assert isinstance(result, pd.DataFrame)

    def test_variable_column_present(self):
        df = self._make_df_with_methods()
        result = _build_audit_df(df, ["CurrentTemperature"], [])
        assert "variable" in result.columns

    def test_one_row_per_variable(self):
        df = self._make_df_with_methods()
        result = _build_audit_df(df, ["CurrentTemperature", "CurrentHumidity"], [])
        assert len(result) == 2

    def test_method_counts_sum_correctly(self):
        df = self._make_df_with_methods(40)
        result = _build_audit_df(df, ["CurrentTemperature"], [])
        row = result[result["variable"] == "CurrentTemperature"].iloc[0]
        # original + interpolation + model + neighbor should equal total rows
        total = row["original"] + row["interpolation"] + row["model"] + row["neighbor"]
        assert total == 40

    def test_missing_variable_excluded(self):
        df = self._make_df_with_methods()
        result = _build_audit_df(df, ["NonExistent"], [])
        assert result.empty

    def test_rainfall_variables_included(self):
        df = self._make_df_with_methods()
        df["RainfallHourly"] = 0.0
        result = _build_audit_df(df, [], ["RainfallHourly"])
        assert "RainfallHourly" in result["variable"].values

    def test_still_missing_count(self):
        tl = pd.date_range("2023-01-01", periods=10, freq="5min")
        df = pd.DataFrame(
            {
                "CurrentTemperature": [np.nan] * 3 + [25.0] * 7,
                "imputation_method":  ["unresolved"] * 3 + ["original"] * 7,
            },
            index=tl,
        )
        result = _build_audit_df(df, ["CurrentTemperature"], [])
        assert result.iloc[0]["still_missing"] == 3


# ===========================================================================
# _finalize_df
# ===========================================================================

class TestFinalizeDF:

    def test_neighbor_columns_dropped(self):
        df = _make_filled_df(neighbor_ids=["N1"])
        result = _finalize_df(df.copy(), ["N1"])
        for col in ["Temp_N1", "CorrTemp_N1"]:
            assert col not in result.columns

    def test_tracking_columns_at_end(self):
        df = _make_filled_df()
        result = _finalize_df(df.copy(), [])
        tracking = ["gap_type", "gap_id", "gap_size", "filled_flag",
                    "imputation_method", "confidence_level"]
        end_cols = list(result.columns)[-len(tracking):]
        assert end_cols == tracking

    def test_non_neighbor_data_columns_preserved(self):
        df = _make_filled_df(neighbor_ids=["N1"])
        result = _finalize_df(df.copy(), ["N1"])
        assert "CurrentTemperature" in result.columns
        assert "CurrentHumidity" in result.columns

    def test_no_neighbors_returns_full_df(self):
        df = _make_filled_df()
        before_ncols = len(df.columns)
        result = _finalize_df(df.copy(), [])
        assert len(result.columns) == before_ncols

    def test_multiple_neighbor_ids_dropped(self):
        df = _make_filled_df(neighbor_ids=["N1", "N2"])
        result = _finalize_df(df.copy(), ["N1", "N2"])
        for nid in ["N1", "N2"]:
            for prefix in ["Temp", "CorrTemp"]:
                assert f"{prefix}_{nid}" not in result.columns

    def test_row_count_unchanged(self):
        df = _make_filled_df(50, neighbor_ids=["N1"])
        result = _finalize_df(df.copy(), ["N1"])
        assert len(result) == 50
