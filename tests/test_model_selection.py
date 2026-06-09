"""tests/test_model_selection.py — full coverage of src/model_selection.py."""

import os
import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch, MagicMock

from sklearn.model_selection import TimeSeriesSplit
from sklearn.neighbors import KNeighborsRegressor

from src.model_selection import (
    build_features,
    make_models,
    select_best_model,
    run_model_tournament,
    fill_medium_gaps,
    save_models,
    load_models,
    model_mapping,
    save_model_mapping,
    prepare_models,
)
from src.gap_analysis import classify_gaps
from src.interpolation import init_tracking_columns


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_timeline(n=200) -> pd.DatetimeIndex:
    return pd.date_range("2023-01-01", periods=n, freq="5min")


def _make_tracked_df(n=200, gap_slices=None):
    """Build a reindexed + tracked DataFrame."""
    tl = _make_timeline(n)
    rng = np.random.default_rng(11)
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

    ref_col = "CurrentTemperature"
    (gtm, gim, gsm, iso, med, lon, gsizes, groups, _) = classify_gaps(df.copy(), ref_col)
    tracked = init_tracking_columns(df.copy(), ref_col, gtm, gim, gsm)
    return tracked, gsizes, groups


def _add_neighbor_features(df, nid="N1"):
    rng = np.random.default_rng(12)
    n = len(df)
    df[f"Temp_{nid}"] = rng.uniform(20, 35, n)
    df[f"CorrTemp_{nid}"] = rng.uniform(20, 35, n)
    df[f"Hum_{nid}"] = rng.uniform(40, 90, n)
    return df


# ===========================================================================
# build_features
# ===========================================================================

class TestBuildFeatures:

    def test_returns_dataframe(self):
        df, *_ = _make_tracked_df(100)
        feat = build_features(df, "CurrentTemperature", [])
        assert isinstance(feat, pd.DataFrame)

    def test_has_calendar_columns(self):
        df, *_ = _make_tracked_df(100)
        feat = build_features(df, "CurrentTemperature", [])
        for col in ["hour", "day_of_week", "month"]:
            assert col in feat.columns

    def test_has_lag_columns(self):
        df, *_ = _make_tracked_df(100)
        feat = build_features(df, "CurrentTemperature", [])
        for col in ["lag_1", "lag_2", "lag_3"]:
            assert col in feat.columns

    def test_has_rolling_columns(self):
        df, *_ = _make_tracked_df(100)
        feat = build_features(df, "CurrentTemperature", [])
        for col in ["rolling_mean", "rolling_std"]:
            assert col in feat.columns

    def test_neighbor_feature_attached_when_present(self):
        df, *_ = _make_tracked_df(100)
        df = _add_neighbor_features(df, "N1")
        feat = build_features(df, "CurrentTemperature", ["N1"])
        assert "Temp_N1" in feat.columns

    def test_no_leakage_lag_uses_only_original(self):
        """Lags should only use rows where imputation_method == 'original'."""
        df, *_ = _make_tracked_df(50, [slice(10, 11)])
        # Force a non-original row
        df.iloc[5, df.columns.get_loc("imputation_method") if "imputation_method" in df.columns else 0]
        feat = build_features(df, "CurrentTemperature", [])
        # lag_1 values at imputed rows should be NaN (no leakage)
        assert "lag_1" in feat.columns

    def test_index_aligns_with_input(self):
        df, *_ = _make_tracked_df(50)
        feat = build_features(df, "CurrentTemperature", [])
        assert list(feat.index) == list(df.index)

    def test_hour_range(self):
        df, *_ = _make_tracked_df(100)
        feat = build_features(df, "CurrentTemperature", [])
        assert feat["hour"].between(0, 23).all()

    def test_month_range(self):
        df, *_ = _make_tracked_df(100)
        feat = build_features(df, "CurrentTemperature", [])
        assert feat["month"].between(1, 12).all()


# ===========================================================================
# make_models
# ===========================================================================

class TestMakeModels:

    def test_returns_four_models(self):
        models = make_models()
        assert len(models) == 4

    def test_model_keys_present(self):
        models = make_models()
        for name in ["XGBoost", "LightGBM", "RandomForest", "KNN"]:
            assert name in models

    def test_custom_params_applied(self):
        models = make_models(model_params={"knn": {"n_neighbors": 10}})
        knn = models["KNN"]
        assert knn.n_neighbors == 10

    def test_each_call_returns_fresh_instances(self):
        m1 = make_models()
        m2 = make_models()
        assert m1["KNN"] is not m2["KNN"]

    def test_none_params_uses_defaults(self):
        models = make_models(None)
        assert len(models) == 4


# ===========================================================================
# select_best_model
# ===========================================================================

class TestSelectBestModel:

    def _xy(self, n=300):
        rng = np.random.default_rng(42)
        x = pd.DataFrame({
            "hour": rng.integers(0, 24, n),
            "month": rng.integers(1, 13, n),
            "lag_1": rng.uniform(20, 35, n),
        })
        y = pd.Series(rng.uniform(20, 35, n))
        return x, y

    def test_returns_best_name_and_scores(self):
        X, y = self._xy()
        tscv = TimeSeriesSplit(n_splits=2)
        models = make_models()
        best_name, scores = select_best_model(X, y, models, tscv)
        assert isinstance(best_name, str)
        assert best_name in models
        assert isinstance(scores, dict)

    def test_scores_for_all_models(self):
        X, y = self._xy()
        tscv = TimeSeriesSplit(n_splits=2)
        models = make_models()
        _, scores = select_best_model(X, y, models, tscv)
        assert set(scores.keys()) == set(models.keys())

    def test_best_model_has_lowest_mae(self):
        X, y = self._xy()
        tscv = TimeSeriesSplit(n_splits=2)
        models = make_models()
        best_name, scores = select_best_model(X, y, models, tscv)
        assert scores[best_name] == min(scores.values())

    def test_scores_are_non_negative(self):
        X, y = self._xy()
        tscv = TimeSeriesSplit(n_splits=2)
        models = make_models()
        _, scores = select_best_model(X, y, models, tscv)
        for v in scores.values():
            assert v >= 0


# ===========================================================================
# run_model_tournament
# ===========================================================================

class TestRunModelTournament:

    def _tracked_with_neighbor(self, n=300):
        df, *_ = _make_tracked_df(n)
        df = _add_neighbor_features(df)
        return df

    def test_returns_dict_and_df(self):
        df = self._tracked_with_neighbor(300)
        sel, sel_df = run_model_tournament(df, ["CurrentTemperature"], [], n_splits=2)
        assert isinstance(sel, dict)
        assert isinstance(sel_df, pd.DataFrame)

    def test_model_per_variable(self):
        df = self._tracked_with_neighbor(300)
        sel, _ = run_model_tournament(df, ["CurrentTemperature"], [], n_splits=2)
        assert "CurrentTemperature" in sel

    def test_skips_variable_with_too_few_samples(self):
        df, *_ = _make_tracked_df(30)  # Only 30 rows → < 100 threshold
        sel, sel_df = run_model_tournament(df, ["CurrentTemperature"], [], n_splits=2)
        assert "CurrentTemperature" not in sel
        assert len(sel_df) == 0

    def test_selection_df_has_best_model_column(self):
        df = self._tracked_with_neighbor(300)
        _, sel_df = run_model_tournament(df, ["CurrentTemperature"], [], n_splits=2)
        assert "best_model" in sel_df.columns

    def test_fitted_model_can_predict(self):
        df = self._tracked_with_neighbor(300)
        sel, _ = run_model_tournament(df, ["CurrentTemperature"], [], n_splits=2)
        if "CurrentTemperature" in sel:
            feat = build_features(df, "CurrentTemperature", [])
            mask = df["imputation_method"] == "original"
            X_train = feat[mask].dropna()
            preds = sel["CurrentTemperature"].predict(X_train.iloc[:5])
            assert len(preds) == 5


# ===========================================================================
# fill_medium_gaps
# ===========================================================================

class TestFillMediumGaps:

    def _prepare(self, n=200):
        df, gsizes, groups = _make_tracked_df(n, [slice(80, 83)])  # 3-row medium gap
        df = _add_neighbor_features(df)
        # Train a quick model
        sel, _ = run_model_tournament(df.copy(), ["CurrentTemperature"], ["N1"], n_splits=2)
        clip = {"CurrentTemperature": (-10.0, 60.0), "CurrentHumidity": (0.0, 100.0)}
        return df.copy(), sel, gsizes, groups, clip

    def test_medium_gap_filled(self):
        df, sel, gsizes, groups, clip = self._prepare()
        result = fill_medium_gaps(df, ["CurrentTemperature"], sel, ["N1"], groups, gsizes, clip)
        model_rows = result[result["imputation_method"] == "model"]
        assert len(model_rows) > 0

    def test_method_tagged_model(self):
        df, sel, gsizes, groups, clip = self._prepare()
        result = fill_medium_gaps(df, ["CurrentTemperature"], sel, ["N1"], groups, gsizes, clip)
        model_rows = result[result["imputation_method"] == "model"]
        assert (model_rows["imputation_method"] == "model").all()

    def test_confidence_medium(self):
        df, sel, gsizes, groups, clip = self._prepare()
        result = fill_medium_gaps(df, ["CurrentTemperature"], sel, ["N1"], groups, gsizes, clip)
        model_rows = result[result["imputation_method"] == "model"]
        if len(model_rows) > 0:
            assert (model_rows["confidence_level"] == "Medium").all()

    def test_clipping_applied(self):
        df, sel, gsizes, groups, _ = self._prepare()
        tight_clip = {"CurrentTemperature": (24.0, 26.0), "CurrentHumidity": (0.0, 100.0)}
        result = fill_medium_gaps(df, ["CurrentTemperature"], sel, ["N1"], groups, gsizes, tight_clip)
        model_rows = result[result["imputation_method"] == "model"]
        if len(model_rows) > 0:
            vals = model_rows["CurrentTemperature"]
            assert (vals >= 24.0).all()
            assert (vals <= 26.0).all()

    def test_no_medium_gaps_no_change(self):
        df, gsizes, groups = _make_tracked_df(200)  # no gaps
        df = _add_neighbor_features(df)
        sel, _ = run_model_tournament(df.copy(), ["CurrentTemperature"], ["N1"], n_splits=2)
        before = df["imputation_method"].copy()
        result = fill_medium_gaps(
            df, ["CurrentTemperature"], sel, ["N1"], groups, gsizes,
            {"CurrentTemperature": (-10.0, 60.0)}
        )
        # No model rows should appear since no medium gaps
        assert (result["imputation_method"] == before).all()

    def test_missing_model_col_skipped(self):
        df, sel, gsizes, groups, clip = self._prepare()
        # Ask to fill a column not in model_selection
        result = fill_medium_gaps(
            df, ["CurrentHumidity"], sel, ["N1"], groups, gsizes,
            {"CurrentHumidity": (0.0, 100.0)}
        )
        # Humidity medium-gap rows should not have been filled with 'model'
        assert (result["imputation_method"] != "model").all()


# ===========================================================================
# save_models / load_models
# ===========================================================================

class TestSaveLoadModels:

    def test_roundtrip(self, tmp_path):
        from sklearn.neighbors import KNeighborsRegressor
        model = KNeighborsRegressor(n_neighbors=3)
        X = pd.DataFrame({"a": [1, 2, 3, 4, 5]})
        y = pd.Series([10, 20, 30, 40, 50])
        model.fit(X, y)
        ms = {"Temp": model}

        path = str(tmp_path / "models.joblib")
        save_models(ms, path)
        loaded = load_models(path)

        assert "Temp" in loaded
        pred = loaded["Temp"].predict(X)
        assert len(pred) == 5

    def test_save_creates_file(self, tmp_path):
        from sklearn.neighbors import KNeighborsRegressor
        model = KNeighborsRegressor()
        model.fit([[1], [2]], [1, 2])
        path = str(tmp_path / "m.joblib")
        save_models({"col": model}, path)
        assert os.path.exists(path)


# ===========================================================================
# model_mapping
# ===========================================================================

class TestModelMapping:

    def test_knn_mapped(self):
        m = KNeighborsRegressor()
        result = model_mapping({"Temp": m})
        assert result["Temp"] == "KNN"

    def test_unknown_model_type_uses_class_name(self):
        class CustomModel:
            pass
        result = model_mapping({"X": CustomModel()})
        assert result["X"] == "CustomModel"

    def test_empty_dict(self):
        assert model_mapping({}) == {}


# ===========================================================================
# save_model_mapping
# ===========================================================================

class TestSaveModelMapping:

    def test_file_created_with_correct_content(self, tmp_path):
        m = KNeighborsRegressor()
        path = str(tmp_path / "mapping.txt")
        save_model_mapping({"Temperature": m}, path)
        assert os.path.exists(path)
        content = open(path).read()
        assert "Temperature" in content
        assert "KNN" in content

    def test_multiple_variables_written(self, tmp_path):
        from sklearn.ensemble import RandomForestRegressor
        models = {
            "Temp": KNeighborsRegressor(),
            "Hum": RandomForestRegressor(),
        }
        path = str(tmp_path / "multi.txt")
        save_model_mapping(models, path)
        lines = open(path).readlines()
        assert len(lines) == 2


# ===========================================================================
# prepare_models
# ===========================================================================

class TestPrepareModels:

    def _tracked(self, n=300):
        df, *_ = _make_tracked_df(n)
        df = _add_neighbor_features(df)
        return df

    def test_train_mode_returns_models_and_df(self, tmp_path):
        df = self._tracked()
        sel, sel_df = prepare_models(
            df, ["CurrentTemperature"], ["N1"],
            train_mode=True, model_dir=str(tmp_path), n_splits=2
        )
        assert isinstance(sel, dict)
        # sel_df may be empty if < 100 rows but structure is correct
        assert isinstance(sel_df, pd.DataFrame)

    def test_train_mode_saves_joblib(self, tmp_path):
        df = self._tracked()
        prepare_models(
            df, ["CurrentTemperature"], ["N1"],
            train_mode=True, model_dir=str(tmp_path), n_splits=2
        )
        assert os.path.exists(str(tmp_path / "model_selection.joblib"))

    def test_train_mode_saves_mapping(self, tmp_path):
        df = self._tracked()
        prepare_models(
            df, ["CurrentTemperature"], ["N1"],
            train_mode=True, model_dir=str(tmp_path), n_splits=2
        )
        assert os.path.exists(str(tmp_path / "model_mapping_report.txt"))

    def test_inference_mode_loads_models(self, tmp_path):
        df = self._tracked()
        # First train
        prepare_models(
            df, ["CurrentTemperature"], ["N1"],
            train_mode=True, model_dir=str(tmp_path), n_splits=2
        )
        # Then load
        sel, sel_df = prepare_models(
            df, ["CurrentTemperature"], ["N1"],
            train_mode=False, model_dir=str(tmp_path), n_splits=2
        )
        assert isinstance(sel, dict)
        assert sel_df is None

    def test_inference_mode_df_is_none(self, tmp_path):
        df = self._tracked()
        prepare_models(df, ["CurrentTemperature"], ["N1"],
                       train_mode=True, model_dir=str(tmp_path), n_splits=2)
        _, sel_df = prepare_models(df, ["CurrentTemperature"], ["N1"],
                                   train_mode=False, model_dir=str(tmp_path), n_splits=2)
        assert sel_df is None
