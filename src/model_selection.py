"""model_selection.py — features, model tournament, medium-gap fill (notebook Steps 8-10).

Tournament candidates: XGBoost, LightGBM, RandomForest, KNN. Best model per variable is
chosen by lowest TimeSeriesSplit mean MAE (chronological, no random split, no AIC).
"""

from typing import Dict, List, Tuple
import os
import warnings
import pandas as pd
import numpy as np
import joblib

from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_absolute_error
from sklearn.ensemble import RandomForestRegressor
from sklearn.neighbors import KNeighborsRegressor
from xgboost import XGBRegressor
from lightgbm import LGBMRegressor

# Predicting on raw numpy arrays (medium-gap loop) triggers a benign sklearn
# "no feature names" warning; silence it without affecting results.
warnings.filterwarnings("ignore", message="X does not have valid feature names")


_DEFAULT_PARAMS = {
    'xgboost': dict(n_estimators=100, max_depth=5, learning_rate=0.05,
                    subsample=0.8, colsample_bytree=0.8, random_state=42, verbosity=0),
    'lightgbm': dict(n_estimators=100, max_depth=5, learning_rate=0.05,
                     subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1),
    'randomforest': dict(n_estimators=100, max_depth=10, random_state=42, n_jobs=-1),
    'knn': dict(n_neighbors=5),
}


def build_features(df: pd.DataFrame, col: str, neighbor_ids: List[str]) -> pd.DataFrame:
    """Build calendar + lag/rolling (shift(1), no leakage) + neighbour features for `col`."""
    f = pd.DataFrame(index=df.index)
    f['hour'] = df.index.hour
    f['day_of_week'] = df.index.dayofweek
    f['month'] = df.index.month
    orig = df[col].where(df['imputation_method'] == 'original')
    f['lag_1'] = orig.shift(1)
    f['lag_2'] = orig.shift(2)
    f['lag_3'] = orig.shift(3)
    f['rolling_mean'] = orig.shift(1).rolling(6, min_periods=3).mean()
    f['rolling_std'] = orig.shift(1).rolling(6, min_periods=3).std()
    for nid in neighbor_ids:
        for s in ['Temp', 'CorrTemp', 'Hum', 'CorrHum', 'Pres', 'Wind']:
            c = f'{s}_{nid}'
            if c in df.columns:
                f[c] = df[c]
    return f


def make_models(model_params: Dict = None) -> Dict:
    """Return a fresh dict of tournament estimators (params merged over defaults)."""
    p = {k: dict(v) for k, v in _DEFAULT_PARAMS.items()}
    if model_params:
        for k, v in model_params.items():
            p[k].update(v)
    return {
        'XGBoost': XGBRegressor(**p['xgboost']),
        'LightGBM': LGBMRegressor(**p['lightgbm']),
        'RandomForest': RandomForestRegressor(**p['randomforest']),
        'KNN': KNeighborsRegressor(**p['knn']),
    }


def select_best_model(
    X: pd.DataFrame, y: pd.Series, models: Dict, tscv: TimeSeriesSplit
) -> Tuple[str, Dict[str, float]]:
    """Score each candidate by TimeSeriesSplit mean MAE; return best name + all scores."""
    best_name, best_mae = None, np.inf
    scores: Dict[str, float] = {}
    for name, model in models.items():
        maes = []
        for tr, te in tscv.split(X):
            model.fit(X.iloc[tr], y.iloc[tr])
            maes.append(mean_absolute_error(y.iloc[te], model.predict(X.iloc[te])))
        mean_mae = float(np.mean(maes))
        scores[name] = round(mean_mae, 4)
        if mean_mae < best_mae:
            best_mae, best_name = mean_mae, name
    return best_name, scores


def run_model_tournament(
    main: pd.DataFrame,
    cont_vars: List[str],
    neighbor_ids: List[str],
    model_params: Dict = None,
    n_splits: int = 3,
) -> Tuple[Dict, pd.DataFrame]:
    """Run the per-variable tournament; fit the winner on all training rows.

    Returns the {variable: fitted_model} dict and the model-selection report frame.
    """
    tscv = TimeSeriesSplit(n_splits=n_splits)
    model_selection: Dict = {}
    selection_rows: List[Dict] = []

    for col in cont_vars:
        feat = build_features(main, col, neighbor_ids)
        train_mask = (main['imputation_method'] == 'original') & main[col].notna() & feat.notna().all(axis=1)
        X = feat[train_mask]
        y = main.loc[train_mask, col]
        if len(X) < 100:
            continue

        best_name, scores = select_best_model(X, y, make_models(model_params), tscv)

        row = {'variable': col}
        for name, mae in scores.items():
            row[f'{name}_MAE'] = mae
        row['best_model'] = best_name

        best_model = make_models(model_params)[best_name]
        best_model.fit(X, y)
        model_selection[col] = best_model
        selection_rows.append(row)

    model_selection_df = pd.DataFrame(selection_rows)
    print("Model Selection Report:")
    print(model_selection_df.to_string(index=False))

    return model_selection, model_selection_df


def fill_medium_gaps(
    main: pd.DataFrame,
    cont_vars: List[str],
    model_selection: Dict,
    neighbor_ids: List[str],
    groups: pd.Series,
    gsizes: pd.Series,
    clip: Dict[str, Tuple[float, float]],
) -> pd.DataFrame:
    """Fill medium (2-5 row) gaps iteratively with the selected model (each pred feeds next lag)."""
    hour_arr = main.index.hour.to_numpy()
    dow_arr = main.index.dayofweek.to_numpy()
    month_arr = main.index.month.to_numpy()
    pos_of = {ts: i for i, ts in enumerate(main.index)}
    med_blocks = [sorted(groups[groups == g].index) for g, s in gsizes.items() if 2 <= s <= 5]

    for col in cont_vars:
        if col not in model_selection:
            continue
        feat = build_features(main, col, neighbor_ids)
        feat_cols = feat.columns.tolist()
        nbr_cols = [c for c in feat_cols if any(c.endswith(f'_{nid}') for nid in neighbor_ids)]
        train_mask = (main['imputation_method'] == 'original') & main[col].notna() & feat.notna().all(axis=1)
        med_vals = feat[train_mask].median().to_dict()

        work = main[col].to_numpy(copy=True)
        nbr_arr = {c: main[c].to_numpy() for c in nbr_cols}
        model = model_selection[col]

        for block in med_blocks:
            for ts in block:
                p = pos_of[ts]
                row = [hour_arr[p], dow_arr[p], month_arr[p],
                       work[p - 1] if p >= 1 else np.nan,
                       work[p - 2] if p >= 2 else np.nan,
                       work[p - 3] if p >= 3 else np.nan,
                       np.nanmean(work[max(0, p - 6):p]) if p > 0 else np.nan,
                       np.nanstd(work[max(0, p - 6):p]) if p > 0 else np.nan]
                for c in nbr_cols:
                    row.append(nbr_arr[c][p])
                arr = np.array(row, dtype=float)
                for i, c in enumerate(feat_cols):
                    if np.isnan(arr[i]):
                        arr[i] = med_vals[c]
                pred = float(np.clip(model.predict(arr.reshape(1, -1))[0], *clip[col]))
                work[p] = pred
                main.loc[ts, col] = pred
                main.loc[ts, 'imputation_method'] = 'model'
                main.loc[ts, 'confidence_level'] = 'Medium'
                main.loc[ts, 'filled_flag'] = 1

    filled = (main['imputation_method'] == 'model').sum()
    print(f"Medium gap rows filled by model: {filled:,}")
    return main


def save_models(model_selection: Dict, path: str) -> None:
    """Persist the {variable: fitted_model} dict to disk via joblib."""
    joblib.dump(model_selection, path)
    print(f"Saved {len(model_selection)} models -> {path}")


def load_models(path: str) -> Dict:
    """Load a previously saved {variable: fitted_model} dict from disk."""
    model_selection = joblib.load(path)
    print(f"Loaded {len(model_selection)} models <- {path}")
    return model_selection


# Friendly model names for the mapping report (resolved from fitted estimators).
_MODEL_NAMES = {
    'XGBRegressor': 'XGBoost',
    'LGBMRegressor': 'LightGBM',
    'RandomForestRegressor': 'RandomForest',
    'KNeighborsRegressor': 'KNN',
}


def model_mapping(model_selection: Dict) -> Dict[str, str]:
    """Map each variable to its winning model's friendly name (works in both modes)."""
    return {var: _MODEL_NAMES.get(type(m).__name__, type(m).__name__)
            for var, m in model_selection.items()}


def save_model_mapping(model_selection: Dict, path: str) -> None:
    """Write a human-readable 'variable -> ModelName' mapping report."""
    mapping = model_mapping(model_selection)
    with open(path, 'w') as f:
        for var, name in mapping.items():
            f.write(f"{var} -> {name}\n")
    print(f"Saved model mapping -> {path}")
    for var, name in mapping.items():
        print(f"  {var} -> {name}")


def prepare_models(
    main: pd.DataFrame,
    cont_vars: List[str],
    neighbor_ids: List[str],
    train_mode: bool,
    model_dir: str,
    model_params: Dict = None,
    n_splits: int = 3,
) -> Tuple[Dict, pd.DataFrame]:
    """Switch between training and inference.

    TRAIN_MODE (train_mode=True):
        run tournament, select+train best model per variable, persist models + mapping report.
    INFERENCE_MODE (train_mode=False):
        load previously saved models; skip tournament and retraining.

    Returns (model_selection, model_selection_df); the df is None in inference mode.
    """
    models_path = os.path.join(model_dir, 'model_selection.joblib')
    mapping_path = os.path.join(model_dir, 'model_mapping_report.txt')

    if train_mode:
        os.makedirs(model_dir, exist_ok=True)
        model_selection, model_selection_df = run_model_tournament(
            main, cont_vars, neighbor_ids, model_params, n_splits)
        save_models(model_selection, models_path)
        save_model_mapping(model_selection, mapping_path)
        return model_selection, model_selection_df

    model_selection = load_models(models_path)
    print("Model mapping (loaded):")
    for var, name in model_mapping(model_selection).items():
        print(f"  {var} -> {name}")
    return model_selection, None
