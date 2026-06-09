"""tests/test_neighbor_reconstruction.py — full coverage of src/neighbor_reconstruction.py."""

import numpy as np
import pandas as pd
import pytest

from src.neighbor_reconstruction import (
    classify_gaps_series,
    load_neighbors,
    compute_reliability,
    reconstruct_long_gaps,
)
from src.gap_analysis import classify_gaps
from src.interpolation import init_tracking_columns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_timeline(n=288, start="2023-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=n, freq="5min")


def _make_main_df(n=288, long_gap_slice=None):
    tl = _make_timeline(n)
    rng = np.random.default_rng(5)
    df = pd.DataFrame(
        {
            "CurrentTemperature": rng.uniform(20, 35, n),
            "CurrentHumidity":    rng.uniform(40, 90, n),
        },
        index=tl,
    )
    df.index.name = "TimeStamp"
    if long_gap_slice:
        df.iloc[long_gap_slice, 0] = np.nan
    return df


def _tracked(df):
    ref_col = "CurrentTemperature"
    (gtm, gim, gsm, iso, med, lon, gsizes, groups, _) = classify_gaps(df.copy(), ref_col)
    tracked = init_tracking_columns(df.copy(), ref_col, gtm, gim, gsm)
    return tracked, iso, med, lon, gsizes, groups


def _write_neighbor_csv(tmp_path, filename, tl, seed=7):
    rng = np.random.default_rng(seed)
    n = len(tl)
    df = pd.DataFrame({
        "TimeStamp":          tl.strftime("%Y-%m-%d %H:%M:%S"),
        "CurrentTemperature": rng.uniform(20, 35, n),
        "CorrectedTemp":      rng.uniform(20, 35, n),
        "CurrentHumidity":    rng.uniform(40, 90, n),
        "CorrectedHumidity":  rng.uniform(40, 90, n),
        "CorrectedHeatIndex": rng.uniform(25, 40, n),
        "AtmPressure":        rng.uniform(990, 1010, n),
        "WindSpeed":          rng.uniform(0, 10, n),
        "RainfallHourly":     np.zeros(n),
    })
    p = tmp_path / filename
    df.to_csv(p, index=False)
    return str(p)


# ===========================================================================
# classify_gaps_series
# ===========================================================================

class TestClassifyGapsSeries:

    def test_no_gaps(self):
        s = pd.Series([1.0] * 20)
        iso, med, lon = classify_gaps_series(s)
        assert iso == 0
        assert med == 0
        assert lon == 0

    def test_all_nan(self):
        s = pd.Series([np.nan] * 20)
        iso, med, lon = classify_gaps_series(s)
        # One contiguous block of 20 → long
        assert lon == 1
        assert iso == 0
        assert med == 0

    def test_isolated_gap(self):
        s = pd.Series([1.0] * 5 + [np.nan] + [1.0] * 5)
        iso, med, lon = classify_gaps_series(s)
        assert iso == 1
        assert med == 0
        assert lon == 0

    def test_medium_gap(self):
        s = pd.Series([1.0] * 5 + [np.nan] * 3 + [1.0] * 5)
        iso, med, lon = classify_gaps_series(s)
        assert med == 1
        assert iso == 0
        assert lon == 0

    def test_long_gap(self):
        s = pd.Series([1.0] * 5 + [np.nan] * 8 + [1.0] * 5)
        iso, med, lon = classify_gaps_series(s)
        assert lon == 1
        assert iso == 0

    def test_mixed_gaps(self):
        # isolated, medium (3), long (8)
        data = ([1.0] * 4 + [np.nan]           # isolated
                + [1.0] * 4 + [np.nan] * 3    # medium
                + [1.0] * 4 + [np.nan] * 8    # long
                + [1.0] * 3)
        s = pd.Series(data)
        iso, med, lon = classify_gaps_series(s)
        assert iso == 1
        assert med == 1
        assert lon == 1

    def test_boundary_exactly_5_is_medium(self):
        s = pd.Series([1.0] * 5 + [np.nan] * 5 + [1.0] * 5)
        iso, med, lon = classify_gaps_series(s)
        assert med == 1
        assert lon == 0

    def test_boundary_exactly_6_is_long(self):
        s = pd.Series([1.0] * 5 + [np.nan] * 6 + [1.0] * 5)
        iso, med, lon = classify_gaps_series(s)
        assert lon == 1
        assert med == 0


# ===========================================================================
# load_neighbors
# ===========================================================================

class TestLoadNeighbors:

    def test_returns_three_items(self, tmp_path):
        tl = _make_timeline(50)
        main = _make_main_df(50)
        p = _write_neighbor_csv(tmp_path, "n1.csv", tl)
        neighbors, main_out, qdf = load_neighbors([p], ["N1"], tl, main.copy())
        assert isinstance(neighbors, dict)
        assert isinstance(main_out, pd.DataFrame)
        assert isinstance(qdf, pd.DataFrame)

    def test_neighbor_keyed_by_id(self, tmp_path):
        tl = _make_timeline(50)
        main = _make_main_df(50)
        p = _write_neighbor_csv(tmp_path, "n1.csv", tl)
        neighbors, _, _ = load_neighbors([p], ["N1"], tl, main.copy())
        assert "N1" in neighbors

    def test_neighbor_aligned_to_timeline(self, tmp_path):
        tl = _make_timeline(50)
        main = _make_main_df(50)
        p = _write_neighbor_csv(tmp_path, "n1.csv", tl)
        neighbors, _, _ = load_neighbors([p], ["N1"], tl, main.copy())
        assert list(neighbors["N1"].index) == list(tl)

    def test_quality_df_has_expected_columns(self, tmp_path):
        tl = _make_timeline(50)
        main = _make_main_df(50)
        p = _write_neighbor_csv(tmp_path, "n1.csv", tl)
        _, _, qdf = load_neighbors([p], ["N1"], tl, main.copy())
        for col in ["neighbor", "coverage_pct", "missing_pct"]:
            assert col in qdf.columns

    def test_feature_columns_attached_to_main(self, tmp_path):
        tl = _make_timeline(50)
        main = _make_main_df(50)
        p = _write_neighbor_csv(tmp_path, "n1.csv", tl)
        _, main_out, _ = load_neighbors([p], ["N1"], tl, main.copy())
        # CorrTemp_N1 should have been attached
        assert "CorrTemp_N1" in main_out.columns

    def test_multiple_neighbors_loaded(self, tmp_path):
        tl = _make_timeline(50)
        main = _make_main_df(50)
        p1 = _write_neighbor_csv(tmp_path, "n1.csv", tl, seed=7)
        p2 = _write_neighbor_csv(tmp_path, "n2.csv", tl, seed=8)
        neighbors, _, qdf = load_neighbors([p1, p2], ["N1", "N2"], tl, main.copy())
        assert "N1" in neighbors
        assert "N2" in neighbors
        assert len(qdf) == 2

    def test_quality_coverage_between_0_and_100(self, tmp_path):
        tl = _make_timeline(50)
        main = _make_main_df(50)
        p = _write_neighbor_csv(tmp_path, "n1.csv", tl)
        _, _, qdf = load_neighbors([p], ["N1"], tl, main.copy())
        assert 0 <= qdf.iloc[0]["coverage_pct"] <= 100

    def test_future_timestamps_removed_from_neighbor(self, tmp_path):
        tl = _make_timeline(5)
        main = _make_main_df(5)
        # Write neighbour with a 2031 timestamp
        df = pd.DataFrame({
            "TimeStamp": list(tl.strftime("%Y-%m-%d %H:%M:%S")) + ["2031-01-01 00:00:00"],
            "CurrentTemperature": [25.0] * 5 + [99.0],
            "CorrectedTemp":      [25.0] * 6,
            "CurrentHumidity":    [60.0] * 6,
            "CorrectedHumidity":  [60.0] * 6,
            "CorrectedHeatIndex": [30.0] * 6,
        })
        p = tmp_path / "future_nbr.csv"
        df.to_csv(p, index=False)
        neighbors, _, _ = load_neighbors([str(p)], ["N1"], tl, main.copy())
        # The aligned frame should only have tl rows
        assert len(neighbors["N1"]) == len(tl)


# ===========================================================================
# compute_reliability
# ===========================================================================

class TestComputeReliability:

    def _setup(self, n=50, cover_pct=1.0, target_miss_pct=0.1, tmp_path=None):
        tl = _make_timeline(n)
        main = _make_main_df(n)
        tracked, *_ = _tracked(main)

        # Attach synthetic neighbour feature columns
        rng = np.random.default_rng(3)
        vals = rng.uniform(20, 35, n)
        if cover_pct < 1.0:
            mask = rng.random(n) > cover_pct
            vals = vals.astype(float)
            vals[mask] = np.nan
        tracked["CorrTemp_N1"] = vals
        tracked["CorrHum_N1"] = rng.uniform(40, 90, n)
        tracked["CorrHeatIdx_N1"] = rng.uniform(25, 40, n)

        # Introduce target missing rows
        n_miss = int(n * target_miss_pct)
        tracked.iloc[:n_miss, 0] = np.nan

        return tracked, tl

    def test_returns_dict_and_df(self, tmp_path):
        tracked, _ = self._setup()
        rel, rel_df = compute_reliability(tracked, ["N1"], [10.0], "CurrentTemperature")
        assert isinstance(rel, dict)
        assert isinstance(rel_df, pd.DataFrame)

    def test_reliability_between_0_and_1(self, tmp_path):
        tracked, _ = self._setup()
        rel, _ = compute_reliability(tracked, ["N1"], [10.0], "CurrentTemperature")
        assert 0.0 <= rel["N1"] <= 1.0

    def test_reliability_df_columns(self, tmp_path):
        tracked, _ = self._setup()
        _, rel_df = compute_reliability(tracked, ["N1"], [10.0], "CurrentTemperature")
        for col in ["neighbor", "reliability", "distance_km"]:
            assert col in rel_df.columns

    def test_multiple_neighbors_scored(self, tmp_path):
        tl = _make_timeline(50)
        main = _make_main_df(50)
        tracked, *_ = _tracked(main)
        rng = np.random.default_rng(4)
        for nid in ["N1", "N2"]:
            tracked[f"CorrTemp_{nid}"] = rng.uniform(20, 35, 50)
            tracked[f"CorrHum_{nid}"] = rng.uniform(40, 90, 50)
            tracked[f"CorrHeatIdx_{nid}"] = rng.uniform(25, 40, 50)
        rel, rel_df = compute_reliability(tracked, ["N1", "N2"], [5.0, 10.0], "CurrentTemperature")
        assert "N1" in rel
        assert "N2" in rel
        assert len(rel_df) == 2

    def test_fallback_to_temp_when_no_corrected_cols(self, tmp_path):
        tl = _make_timeline(50)
        main = _make_main_df(50)
        tracked, *_ = _tracked(main)
        rng = np.random.default_rng(9)
        tracked["Temp_N1"] = rng.uniform(20, 35, 50)
        rel, _ = compute_reliability(tracked, ["N1"], [5.0], "CurrentTemperature")
        assert 0.0 <= rel["N1"] <= 1.0


# ===========================================================================
# reconstruct_long_gaps
# ===========================================================================

class TestReconstructLongGaps:

    def _full_setup(self, tmp_path, n=100, gap_slice=slice(40, 55)):
        tl = _make_timeline(n)
        main = _make_main_df(n, long_gap_slice=gap_slice)
        tracked, iso, med, lon, gsizes, groups = _tracked(main)

        # Load neighbour
        p = _write_neighbor_csv(tmp_path, "n1.csv", tl)
        from src.neighbor_reconstruction import load_neighbors, compute_reliability
        neighbors, tracked, qdf = load_neighbors([p], ["N1"], tl, tracked)
        reliability, _ = compute_reliability(tracked, ["N1"], [5.0], "CurrentTemperature")
        return tracked, neighbors, reliability, gsizes, groups, tl

    def test_long_gap_partially_filled(self, tmp_path):
        tracked, neighbors, reliability, gsizes, groups, tl = self._full_setup(tmp_path)
        cont_vars = ["CurrentTemperature"]
        clip = {"CurrentTemperature": (-10.0, 60.0)}
        result = reconstruct_long_gaps(
            tracked, neighbors, reliability, ["N1"], [5.0],
            cont_vars, tl, groups, gsizes, clip
        )
        # After reconstruction, long gap rows should have imputation_method == 'neighbor' or values
        long_rows = result[result["gap_type"] == "long"]
        # At least some should be filled
        filled = long_rows["imputation_method"].isin(["neighbor"])
        assert filled.sum() > 0

    def test_no_modification_to_short_gaps(self, tmp_path):
        """Medium and isolated gaps must not be touched by reconstruct_long_gaps."""
        n = 100
        tl = _make_timeline(n)
        main = _make_main_df(n)
        # Introduce isolated + medium + long
        main.iloc[5, 0] = np.nan           # isolated
        main.iloc[20:23, 0] = np.nan       # medium
        main.iloc[60:75, 0] = np.nan       # long
        tracked, iso, med, lon, gsizes, groups = _tracked(main)

        p = _write_neighbor_csv(tmp_path, "n1.csv", tl)
        from src.neighbor_reconstruction import load_neighbors, compute_reliability
        neighbors, tracked, _ = load_neighbors([p], ["N1"], tl, tracked)
        reliability, _ = compute_reliability(tracked, ["N1"], [5.0], "CurrentTemperature")

        clip = {"CurrentTemperature": (-10.0, 60.0)}
        result = reconstruct_long_gaps(
            tracked, neighbors, reliability, ["N1"], [5.0],
            ["CurrentTemperature"], tl, groups, gsizes, clip
        )
        # Isolated row at index 5 should still be NaN (not touched by this function)
        assert pd.isna(result.iloc[5]["CurrentTemperature"])
        # Medium rows 20-22 should still be NaN
        for i in range(20, 23):
            assert pd.isna(result.iloc[i]["CurrentTemperature"])

    def test_filled_flag_set_for_reconstructed_rows(self, tmp_path):
        tracked, neighbors, reliability, gsizes, groups, tl = self._full_setup(tmp_path)
        result = reconstruct_long_gaps(
            tracked, neighbors, reliability, ["N1"], [5.0],
            ["CurrentTemperature"], tl, groups, gsizes,
            {"CurrentTemperature": (-10.0, 60.0)}
        )
        nbr_rows = result[result["imputation_method"] == "neighbor"]
        assert (nbr_rows["filled_flag"] == 1).all()

    def test_clipping_respected(self, tmp_path):
        tracked, neighbors, reliability, gsizes, groups, tl = self._full_setup(tmp_path)
        clip = {"CurrentTemperature": (22.0, 28.0)}
        result = reconstruct_long_gaps(
            tracked, neighbors, reliability, ["N1"], [5.0],
            ["CurrentTemperature"], tl, groups, gsizes, clip
        )
        nbr_vals = result.loc[result["imputation_method"] == "neighbor", "CurrentTemperature"]
        if len(nbr_vals) > 0:
            assert (nbr_vals >= 22.0).all()
            assert (nbr_vals <= 28.0).all()

    def test_no_gaps_no_change(self, tmp_path):
        n = 50
        tl = _make_timeline(n)
        main = _make_main_df(n)  # no gaps
        tracked, iso, med, lon, gsizes, groups = _tracked(main)
        p = _write_neighbor_csv(tmp_path, "n1.csv", tl)
        from src.neighbor_reconstruction import load_neighbors, compute_reliability
        neighbors, tracked, _ = load_neighbors([p], ["N1"], tl, tracked)
        reliability, _ = compute_reliability(tracked, ["N1"], [5.0], "CurrentTemperature")

        before_methods = tracked["imputation_method"].copy()
        result = reconstruct_long_gaps(
            tracked, neighbors, reliability, ["N1"], [5.0],
            ["CurrentTemperature"], tl, groups, gsizes,
            {"CurrentTemperature": (-10.0, 60.0)}
        )
        # No long gaps → no neighbour fills
        assert (result["imputation_method"] == before_methods).all()
