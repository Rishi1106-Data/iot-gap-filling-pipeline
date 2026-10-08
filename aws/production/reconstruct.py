"""
reconstruct.py — The reconstruction pipeline, preserved from the notebook.

The logic here is a faithful port of Annam_multimodel_pipeline_PRODUCTION.ipynb:
interval auto-detection, gap classification, isolated interpolation, per-variable
model tournament, medium-gap iterative model fill, long-gap neighbor blending,
rainfall handling, continuity correction, and report generation.

The only change from the notebook is the data source: target + neighbor frames
are passed in (from DynamoDB) rather than read from CSV.
"""

import logging
import os
import time

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBRegressor
from lightgbm import LGBMRegressor

# joblib ships with scikit-learn; used only for the optional persisted-model
# store (Optimisation 5). Guarded so its absence can never break a run.
try:
    import joblib
except Exception:  # pragma: no cover
    joblib = None

import config

log = logging.getLogger("annam.reconstruct")


# ──────────────────────────────────────────────────────────────────────────────
# Interval detection (ported from the notebook)
# ──────────────────────────────────────────────────────────────────────────────
def detect_interval_min(timestamps, fallback=config.DEFAULT_INTERVAL_MIN):
    ts = pd.Series(pd.to_datetime(timestamps, errors="coerce")).dropna()
    ts = ts.drop_duplicates().sort_values()
    if len(ts) < 2:
        return float(fallback), f"{int(fallback)}min"
    gaps = ts.diff().dt.total_seconds().div(60).dropna()
    gaps = gaps[gaps > 0]
    if gaps.empty:
        return float(fallback), f"{int(fallback)}min"
    mode_vals = gaps.round(3).mode()
    if mode_vals.empty:
        return float(fallback), f"{int(fallback)}min"
    interval = float(mode_vals.iloc[0])
    if interval <= 0:
        return float(fallback), f"{int(fallback)}min"
    if abs(interval - round(interval)) < 1e-6:
        freq = f"{int(round(interval))}min"
    else:
        freq = f"{int(round(interval * 60))}s"
    return interval, freq


# ──────────────────────────────────────────────────────────────────────────────
# Fast gap detection (Improvement 1)
#   Runs on the already-loaded target frame BEFORE neighbor loading / model
#   training, so the orchestrator can skip a sensor that has nothing to fill.
#   Uses the SAME interval detection + gridding as run_reconstruction() so the
#   decision can never diverge from what reconstruction would actually do.
#   Fails safe: on any uncertainty it returns has_work=True (run the full path).
# ──────────────────────────────────────────────────────────────────────────────
def analyze_target_gaps(target_df: pd.DataFrame) -> dict:
    """Cheaply decide whether a sensor needs reconstruction at all.

    A sensor has "work" iff, after gridding to the detected interval, the
    reference column has any missing intervals OR any present rainfall column
    has a NaN (rainfall is the only variable filled independently of REF gaps).
    When both are absent, run_reconstruction fills nothing, so the sensor can
    be reconstructed without loading neighbors or training models.

    Returns {"has_work", "has_ref_gaps", "has_rain_nan"}.
    """
    REF = config.REF_COL
    try:
        if target_df is None or target_df.empty or REF not in target_df.columns:
            return {"has_work": True, "has_ref_gaps": None, "has_rain_nan": None}

        try:
            _, FREQ = detect_interval_min(target_df["TimeStamp"])
        except Exception:
            FREQ = f"{config.DEFAULT_INTERVAL_MIN}min"

        df = target_df.copy()
        df["TimeStamp"] = df["TimeStamp"].dt.round(FREQ)
        df = (df.sort_values("TimeStamp")
                .drop_duplicates("TimeStamp", keep="first")
                .reset_index(drop=True))
        if df.empty:
            return {"has_work": True, "has_ref_gaps": None, "has_rain_nan": None}

        timeline = pd.date_range(df["TimeStamp"].min(), df["TimeStamp"].max(), freq=FREQ)
        main = df.set_index("TimeStamp").reindex(timeline)

        has_ref_gaps = bool(main[REF].isna().any())
        rain_cols = [c for c in config.RAIN_VARS if c in main.columns]
        has_rain_nan = bool(main[rain_cols].isna().any().any()) if rain_cols else False

        return {
            "has_work": bool(has_ref_gaps or has_rain_nan),
            "has_ref_gaps": has_ref_gaps,
            "has_rain_nan": has_rain_nan,
        }
    except Exception as exc:
        # On any unexpected error, do NOT skip — preserve today's behaviour by
        # running the full pipeline.
        log.warning("analyze_target_gaps failed (%s); running full pipeline.", exc)
        return {"has_work": True, "has_ref_gaps": None, "has_rain_nan": None}


def _make_model(name):
    """Construct a single fresh estimator by name.

    Hyper-parameters are byte-identical to the originals, so a model built here
    is indistinguishable from the one the tournament would have built for the
    same variable. Used by both the production path (one predetermined model)
    and the offline tournament.
    """
    if name == "XGBoost":
        return XGBRegressor(n_estimators=100, max_depth=5, learning_rate=0.05,
                            subsample=0.8, colsample_bytree=0.8,
                            random_state=42, verbosity=0)
    if name == "LightGBM":
        return LGBMRegressor(n_estimators=100, max_depth=5, learning_rate=0.05,
                             subsample=0.8, colsample_bytree=0.8,
                             random_state=42, verbose=-1)
    if name == "RandomForest":
        return RandomForestRegressor(n_estimators=100, max_depth=10,
                                     random_state=42, n_jobs=-1)
    raise ValueError(f"Unknown model name: {name!r}")


def _make_models():
    # Preserved for the offline tournament (PRODUCTION_MODE=false). Same objects
    # and order as before.
    return {name: _make_model(name)
            for name in ("XGBoost", "LightGBM", "RandomForest")}


# ---------------------------------------------------------------------------
# Persisted-model store (Optimisation 5) -- infrastructure only, OFF by default.
#   A model is keyed by (model_key, variable). When PERSIST_MODELS_ENABLED and a
#   model_key is supplied, run_reconstruction loads an existing model instead of
#   retraining, or trains-then-saves when none exists. No retraining cadence is
#   hardcoded here; clearing MODEL_STORE_DIR is what forces a retrain.
# ---------------------------------------------------------------------------
def _model_path(model_key, col):
    safe_key = "".join(c if (c.isalnum() or c in "-_.") else "_"
                       for c in str(model_key))
    safe_col = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(col))
    return os.path.join(config.MODEL_STORE_DIR, f"{safe_key}__{safe_col}.pkl")


def _load_persisted_model(model_key, col):
    if joblib is None or not model_key:
        return None
    path = _model_path(model_key, col)
    if not os.path.exists(path):
        return None
    try:
        model = joblib.load(path)
        log.info("Loaded persisted model for '%s' from %s", col, path)
        return model
    except Exception as exc:
        log.warning("Failed to load persisted model %s (%s); will retrain.",
                    path, exc)
        return None


def _save_persisted_model(model_key, col, model):
    if joblib is None or not model_key:
        return
    try:
        os.makedirs(config.MODEL_STORE_DIR, exist_ok=True)
        joblib.dump(model, _model_path(model_key, col))
    except Exception as exc:
        log.warning("Failed to persist model for '%s' (%s).", col, exc)


def run_reconstruction(target_df: pd.DataFrame, neighbor_frames: list,
                       model_key: str = None) -> dict:
    """Run the full pipeline.

    target_df        : cleaned target frame (TimeStamp + weather cols)
    neighbor_frames  : list of dicts {id, dist_km, df}
    model_key        : optional identifier (e.g. device id) used only by the
                       persisted-model store (Optimisation 5). Ignored unless
                       config.PERSIST_MODELS_ENABLED is true; None disables
                       persistence entirely (default, unchanged behaviour).
    Returns dict with final_df and report DataFrames.
    """
    REF = config.REF_COL
    NEIGHBOR_IDS = [n["id"] for n in neighbor_frames]
    NEIGHBOR_DISTANCES_KM = [n["dist_km"] for n in neighbor_frames]

    # ── interval detection ────────────────────────────────────────────────────
    try:
        INTERVAL_MIN, FREQ = detect_interval_min(target_df["TimeStamp"])
    except Exception:
        INTERVAL_MIN, FREQ = float(config.DEFAULT_INTERVAL_MIN), f"{config.DEFAULT_INTERVAL_MIN}min"
    log.info("Detected sampling interval: %g minutes", INTERVAL_MIN)

    # ── target: round to grid, dedup, reindex ────────────────────────────────
    df = target_df.copy()
    df["TimeStamp"] = df["TimeStamp"].dt.round(FREQ)
    df = (df.sort_values("TimeStamp")
            .drop_duplicates("TimeStamp", keep="first")
            .reset_index(drop=True))

    if df.empty or REF not in df.columns:
        raise ValueError("Target has no usable rows or missing reference column.")

    timeline = pd.date_range(df["TimeStamp"].min(), df["TimeStamp"].max(), freq=FREQ)
    main = df.set_index("TimeStamp").reindex(timeline)
    main.index.name = "TimeStamp"

    # ── gap classification ────────────────────────────────────────────────────
    is_miss = main[REF].isna()
    groups = (is_miss != is_miss.shift()).cumsum()[is_miss]
    gsizes = groups.value_counts()

    gap_type_map, gap_id_map, gap_size_map = {}, {}, {}
    iso_idx, med_idx, long_idx = set(), set(), set()
    for gid, size in gsizes.items():
        idx_set = set(groups[groups == gid].index)
        if size == 1:
            gtype, bkt = "isolated", iso_idx
        elif size <= 5:
            gtype, bkt = "medium", med_idx
        else:
            gtype, bkt = "long", long_idx
        bkt.update(idx_set)
        for ts in idx_set:
            gap_type_map[ts] = gtype
            gap_id_map[ts] = int(gid)
            gap_size_map[ts] = size

    # Whether any gap needs ML reconstruction (medium gaps, size 2..5). Used to
    # skip the model tournament + medium-gap fill entirely when absent
    # (Improvement 2). Models are never applied without medium gaps, so skipping
    # training here does not change any reconstructed value.
    has_medium_gaps = bool(((gsizes >= 2) & (gsizes <= 5)).any()) if not gsizes.empty else False

    # Phase timing accumulators (Improvement 3). Reported back in the result so
    # the (INFO-level) orchestrator can log them without raising this module's
    # own log level and flooding CloudWatch.
    t_feature_eng = [0.0]   # seconds spent building feature frames
    t_model_train = 0.0     # seconds spent in CV tournament + final fits
    _t_recon0 = None        # set below, wraps isolated+medium+long+rain+continuity

    # ── attach neighbors + quality report ─────────────────────────────────────
    def classify_gaps(series):
        m = series.isna()
        g = (m != m.shift()).cumsum()[m]
        s = g.value_counts()
        return int((s == 1).sum()), int(((s >= 2) & (s <= 5)).sum()), int((s > 5).sum())

    neighbors, neighbor_quality = {}, []
    for n in neighbor_frames:
        nid, nd_raw = n["id"], n["df"]
        if nd_raw is None or nd_raw.empty:
            continue
        nd = nd_raw.copy()
        nd["TimeStamp"] = nd["TimeStamp"].dt.round(FREQ)
        nd = (nd.drop_duplicates("TimeStamp").sort_values("TimeStamp")
                .set_index("TimeStamp").reindex(timeline))
        neighbors[nid] = nd
        if "CurrentTemperature" in nd.columns:
            present = nd["CurrentTemperature"].notna().sum()
            iso_n, med_n, long_n = classify_gaps(nd["CurrentTemperature"])
            neighbor_quality.append({
                "neighbor": nid, "rows_present": int(present),
                "coverage_pct": round(100 * present / len(timeline), 2),
                "missing_pct": round(100 * (1 - present / len(timeline)), 2),
                "isolated_gaps": iso_n, "medium_gaps": med_n, "long_gaps": long_n,
            })
        for orig, short in config.NEIGHBOR_FEATURE_MAP.items():
            if orig in nd.columns:
                main[f"{short}_{nid}"] = nd[orig]

    # keep only neighbors we actually loaded
    NEIGHBOR_IDS = [nid for nid in NEIGHBOR_IDS if nid in neighbors]
    dist_lookup = {n["id"]: n["dist_km"] for n in neighbor_frames}

    # ── reliability ───────────────────────────────────────────────────────────
    target_present = main[REF].notna()
    reliability = {}
    for nid in NEIGHBOR_IDS:
        col = f"Temp_{nid}"
        if col not in main.columns:
            reliability[nid] = 0.0
            continue
        nbr_present = main[col].notna()
        coverage = nbr_present.mean()
        target_miss = ~target_present
        overlap = (nbr_present & target_miss).sum() / max(target_miss.sum(), 1)
        reliability[nid] = round(0.6 * coverage + 0.4 * overlap, 4)

    # ── tracking columns ──────────────────────────────────────────────────────
    main["gap_type"] = main.index.map(lambda t: gap_type_map.get(t, "original"))
    main["gap_id"] = main.index.map(lambda t: gap_id_map.get(t, 0))
    main["gap_size"] = main.index.map(lambda t: gap_size_map.get(t, 0))
    main["imputation_method"] = np.where(main[REF].notna(), "original", "unresolved")
    main["confidence_level"] = np.where(main[REF].notna(), "High", "unknown")
    main["filled_flag"] = np.where(main[REF].notna(), 0, 1)

    CONT_VARS = [c for c in config.CONT_VARS if c in main.columns]
    CLIP = config.CLIP

    # ── isolated gaps: time interpolation, limit 1 ────────────────────────────
    t_recon = 0.0
    _t_recon = time.perf_counter()
    iso_pos = pd.Series(False, index=main.index)
    for ts in iso_idx:
        iso_pos[ts] = True
    for col in CONT_VARS:
        non_iso = main[col].isna() & ~iso_pos
        main.loc[non_iso, col] = -99999.0
        main[col] = main[col].replace(-99999.0, np.nan).interpolate(
            method="time", limit=1, limit_area="inside")
        main.loc[non_iso & main[col].notna(), col] = np.nan
    iso_filled = iso_pos & main[REF].notna()
    main.loc[iso_filled, "imputation_method"] = "interpolation"
    main.loc[iso_filled, "confidence_level"] = "High"
    main.loc[iso_filled, "filled_flag"] = 1
    t_recon += time.perf_counter() - _t_recon

    # ── feature builder ───────────────────────────────────────────────────────
    # Feature-matrix cache (Optimisation 2). The feature frame for a variable
    # depends only on ORIGINAL observations (imputation_method == "original")
    # and the static neighbour columns -- neither changes once isolated gaps are
    # filled (before any model runs), so the matrix is identical every time it
    # is requested for a given column. Building it once per variable and reusing
    # it for training, median computation, and the medium-gap fill removes the
    # duplicate feature-engineering pass without altering any feature value or
    # equation. Keyed by column name; main is the only frame passed as df_.
    _feature_cache = {}

    def build_features(df_, col):
        cached = _feature_cache.get(col)
        if cached is not None:
            return cached
        _t = time.perf_counter()
        f = pd.DataFrame(index=df_.index)
        f["hour"] = df_.index.hour
        f["day_of_week"] = df_.index.dayofweek
        f["month"] = df_.index.month
        orig = df_[col].where(df_["imputation_method"] == "original")
        f["lag_1"] = orig.shift(1)
        f["lag_2"] = orig.shift(2)
        f["lag_3"] = orig.shift(3)
        f["rolling_mean"] = orig.shift(1).rolling(6, min_periods=3).mean()
        f["rolling_std"] = orig.shift(1).rolling(6, min_periods=3).std()
        for nid in NEIGHBOR_IDS:
            for s in ["Temp", "CorrTemp", "Hum", "CorrHum", "Pres", "Wind"]:
                c = f"{s}_{nid}"
                if c in df_.columns:
                    f[c] = df_[c]
        t_feature_eng[0] += time.perf_counter() - _t
        _feature_cache[col] = f
        return f

    # Required lag features for model execution (Improvement 4). If a sensor's
    # feature frame is missing any of these, model execution is skipped for the
    # whole sensor rather than raising a KeyError that would fail the task.
    REQUIRED_LAGS = ("lag_1", "lag_2", "lag_3")

    # ── model tournament ──────────────────────────────────────────────────────
    # Skip the entire tournament (and its training cost) when there are no
    # medium gaps to reconstruct (Improvement 2). Without medium gaps no model
    # is ever applied, so this changes no reconstructed value — only the
    # (diagnostic) model_selection_report becomes empty.
    #
    # PRODUCTION_MODE (Phase 3):
    #   * Optimisation 1 -- no tournament. Each variable's model is looked up
    #     from config.PRODUCTION_MODEL_MAP and trained once. No alternative
    #     models are built and no performance comparison is done.
    #   * Optimisation 3 -- no cross-validation. The chosen model is fit once on
    #     all training rows, byte-identical to the tournament's final refit (CV
    #     only ever picked the winner; it never changed the fitted model). So the
    #     reconstructed values are identical whenever the predetermined model
    #     equals the model the tournament would have picked -- the premise of
    #     this optimisation (these are the validated best models per variable).
    #   * Optimisation 4 -- train only variables that actually need it: a
    #     variable with no missing value at any medium-gap timestamp is skipped.
    #   * Optimisation 5 -- reuse a persisted model instead of retraining when
    #     the store has one (config.PERSIST_MODELS_ENABLED + model_key).
    # Offline (PRODUCTION_MODE=false): the original tournament + TimeSeriesSplit
    # runs unchanged.
    production = getattr(config, "PRODUCTION_MODE", True)
    persist = getattr(config, "PERSIST_MODELS_ENABLED", False)
    train_only_required = getattr(config, "TRAIN_ONLY_REQUIRED_VARS", True)
    tscv = TimeSeriesSplit(n_splits=3)
    model_selection, selection_rows = {}, []
    if has_medium_gaps:
        _t_train = time.perf_counter()
        for col in CONT_VARS:
            # Optimisation 4: decide whether this variable needs a model BEFORE
            # any feature engineering. A variable with no missing value at any
            # medium-gap timestamp is skipped entirely -- no feature matrix is
            # built and no model is trained for it. Under this pipeline's
            # whole-row gap semantics every continuous variable is missing at a
            # reference gap, so nothing is skipped in practice (output
            # unchanged); the guard only avoids wasted feature-engineering and
            # training on partial-schema tables.
            if production and train_only_required and med_idx:
                if not main.loc[list(med_idx), col].isna().any():
                    continue

            feat = build_features(main, col)

            # Defensive check (Improvement 4): ensure lag features exist before
            # training. If not, warn, skip model execution for this sensor, and
            # continue — the batch must not be terminated by one sensor.
            missing = [lg for lg in REQUIRED_LAGS if lg not in feat.columns]
            if missing:
                log.warning("Missing lag feature(s) %s for variable '%s'; "
                            "skipping model execution for this sensor.",
                            missing, col)
                model_selection.clear()
                selection_rows.clear()
                break

            train_mask = ((main["imputation_method"] == "original")
                          & main[col].notna() & feat.notna().all(axis=1))
            X, y = feat[train_mask], main.loc[train_mask, col]
            if len(X) < 100:
                continue

            if production:
                # Optimisation 1 + 3: single predetermined model, no CV.
                chosen = config.PRODUCTION_MODEL_MAP.get(
                    col, config.DEFAULT_PRODUCTION_MODEL)
                row = {"variable": col, "best_model": chosen, "mode": "production"}
                # Optimisation 5: reuse a persisted model when available.
                model = _load_persisted_model(model_key, col) if persist else None
                if model is not None:
                    row["mode"] = "production_loaded"
                else:
                    model = _make_model(chosen)
                    model.fit(X, y)
                    if persist:
                        _save_persisted_model(model_key, col, model)
                model_selection[col] = model
                selection_rows.append(row)
            else:
                # Offline experimentation: unchanged tournament + TimeSeriesSplit.
                best_name, best_mae, row = None, np.inf, {"variable": col}
                for name, model in _make_models().items():
                    maes = []
                    for tr, te in tscv.split(X):
                        model.fit(X.iloc[tr], y.iloc[tr])
                        maes.append(mean_absolute_error(y.iloc[te], model.predict(X.iloc[te])))
                    mean_mae = float(np.mean(maes))
                    row[f"{name}_MAE"] = round(mean_mae, 4)
                    if mean_mae < best_mae:
                        best_mae, best_name = mean_mae, name
                best_model = _make_models()[best_name]
                best_model.fit(X, y)
                model_selection[col] = best_model
                row["best_model"] = best_name
                selection_rows.append(row)
        t_model_train = time.perf_counter() - _t_train

    # ── medium gaps: iterative model fill ─────────────────────────────────────
    _t_recon = time.perf_counter()
    hour_arr = main.index.hour.to_numpy()
    dow_arr = main.index.dayofweek.to_numpy()
    month_arr = main.index.month.to_numpy()
    pos_of = {ts: i for i, ts in enumerate(main.index)}
    med_blocks = [sorted(groups[groups == g].index) for g, s in gsizes.items() if 2 <= s <= 5]

    for col in CONT_VARS:
        if col not in model_selection:
            continue
        feat = build_features(main, col)
        feat_cols = feat.columns.tolist()
        nbr_cols = [c for c in feat_cols if any(c.endswith(f"_{nid}") for nid in NEIGHBOR_IDS)]
        train_mask = ((main["imputation_method"] == "original")
                      & main[col].notna() & feat.notna().all(axis=1))
        med_vals = feat[train_mask].median().to_dict()
        work = main[col].to_numpy(copy=True)
        nbr_arr = {c: main[c].to_numpy() for c in nbr_cols}
        model = model_selection[col]
        for block in med_blocks:
            for ts in block:
                p = pos_of[ts]
                rowvals = [hour_arr[p], dow_arr[p], month_arr[p],
                           work[p - 1] if p >= 1 else np.nan,
                           work[p - 2] if p >= 2 else np.nan,
                           work[p - 3] if p >= 3 else np.nan,
                           np.nanmean(work[max(0, p - 6):p]) if p > 0 else np.nan,
                           np.nanstd(work[max(0, p - 6):p]) if p > 0 else np.nan]
                for c in nbr_cols:
                    rowvals.append(nbr_arr[c][p])
                arr = np.array(rowvals, dtype=float)
                for i, c in enumerate(feat_cols):
                    if np.isnan(arr[i]):
                        arr[i] = med_vals.get(c, 0.0)
                pred = float(np.clip(model.predict(arr.reshape(1, -1))[0], *CLIP[col]))
                work[p] = pred
                main.loc[ts, col] = pred
                main.loc[ts, "imputation_method"] = "model"
                main.loc[ts, "confidence_level"] = "Medium"
                main.loc[ts, "filled_flag"] = 1

    # ── long gaps: neighbor blend ─────────────────────────────────────────────
    inv_dist = {nid: 1.0 / dist_lookup[nid] if dist_lookup.get(nid) else 0.0
                for nid in NEIGHBOR_IDS}

    def neighbor_weight(nid):
        return reliability.get(nid, 0.0) * inv_dist.get(nid, 0.0)

    def reconstruct_long(gap_ts, col):
        block_ts = sorted(gap_ts)
        sp = timeline.get_loc(block_ts[0]); ep = timeline.get_loc(block_ts[-1])
        size = len(block_ts)
        p = main.iloc[sp - 1][col] if sp > 0 else np.nan
        n = main.iloc[ep + 1][col] if ep < len(timeline) - 1 else np.nan
        recs, wts = {}, {}
        for nid in NEIGHBOR_IDS:
            nd = neighbors.get(nid)
            if nd is None or col not in nd.columns:
                continue
            npv = nd.iloc[sp - 1][col] if sp > 0 else np.nan
            nnv = nd.iloc[ep + 1][col] if ep < len(timeline) - 1 else np.nan
            ng = nd.loc[block_ts, col].values
            cov = int(np.sum(~np.isnan(ng)))
            if cov == 0:
                continue
            nf = np.concatenate([[npv], ng, [nnv]])
            v = ng[~np.isnan(ng)]
            if np.isnan(nf[0]):
                nf[0] = v[0] if len(v) else np.nan
            if np.isnan(nf[-1]):
                nf[-1] = v[-1] if len(v) else np.nan
            if np.isnan(nf[0]) or np.isnan(nf[-1]):
                continue
            ni = pd.Series(nf).interpolate().values
            mn, mx = ni.min(), ni.max()
            if not np.isnan(p) and not np.isnan(n):
                fracs = (np.linspace(0, 1, size + 2) if abs(mx - mn) < 0.3
                         else (ni - mn) / (mx - mn))
                recon = p + fracs[1:-1] * (n - p)
            else:
                recon = ni[1:-1]
            recs[nid] = recon
            wts[nid] = neighbor_weight(nid) * (cov / size)
        if not recs:
            return np.full(size, np.nan), "unresolved", "unknown"
        tw = sum(wts.values()) or 1.0
        blended = sum(recs[k] * wts[k] / tw for k in recs)
        blended = np.clip(blended, *CLIP[col])
        conf = "Medium" if len(recs) == 2 else "Low"
        return blended, "neighbor", conf

    for gid, size in gsizes.items():
        if size <= 5:
            continue
        gap_ts = groups[groups == gid].index.tolist()
        for col in CONT_VARS:
            vals, method, conf = reconstruct_long(gap_ts, col)
            for i, ts in enumerate(sorted(gap_ts)):
                if not np.isnan(vals[i]):
                    main.loc[ts, col] = vals[i]
                    main.loc[ts, "imputation_method"] = method
                    main.loc[ts, "confidence_level"] = conf
                    main.loc[ts, "filled_flag"] = 1

    # ── rainfall: conservative, evidence-based zero-fill ──────────────────────
    # A missing rainfall value becomes 0 only when there is real evidence of a dry
    # period: at least one OBSERVED value within +/-3 slots of the target series
    # or from a neighbour at the same slot, and none of the observed values is
    # positive. Missing values are never read as "dry", and zeros assigned here
    # are not reused as evidence for later slots (the window is read from a
    # snapshot of genuine observations), so one observation cannot spread a zero
    # across a long gap. Slots that were not reconstructed (not original, and the
    # reference value is still missing) never receive a rainfall value.
    RAIN_VARS = [c for c in config.RAIN_VARS if c in main.columns]
    unresolved_slot = (main["imputation_method"] != "original") & main[REF].isna()
    nbr_rain_cols = [f"RainH_{nid}" for nid in NEIGHBOR_IDS
                     if f"RainH_{nid}" in main.columns]
    for col in RAIN_VARS:
        observed = main[col].to_numpy(dtype=float, copy=True)
        for ts in main.index[main[col].isna() & ~unresolved_slot]:
            pos = main.index.get_loc(ts)
            evidence = [v for v in observed[max(0, pos - 3):min(len(main), pos + 4)]
                        if not np.isnan(v)]
            evidence += [float(main.loc[ts, c]) for c in nbr_rain_cols
                         if pd.notna(main.loc[ts, c])]
            if evidence and max(evidence) <= 0:
                main.loc[ts, col] = 0

    # ── continuity correction ─────────────────────────────────────────────────
    non_orig = main["imputation_method"] != "original"
    for col, thresh in config.JUMP.items():
        if col not in main.columns:
            continue
        dprev = (main[col] - main[col].shift(1)).abs()
        dnext = (main[col] - main[col].shift(-1)).abs()
        bad = non_orig & ((dprev > thresh) | (dnext > thresh))
        for ts in main.index[bad]:
            pos = main.index.get_loc(ts)
            nb_vals = [main.iloc[pos - 1][col] if pos > 0 else np.nan,
                       main.iloc[pos + 1][col] if pos < len(main) - 1 else np.nan]
            nb_vals = [v for v in nb_vals if not pd.isna(v)]
            if nb_vals:
                main.loc[ts, col] = 0.6 * main.loc[ts, col] + 0.4 * np.mean(nb_vals)

    # filled_flag = 1 means "this row is not an original observation" (it may be
    # reconstructed OR unresolved). It is NOT a persistence decision: the production
    # writer uses io_dynamo.reconstruction_masks() (method + reference value) for that.
    main["filled_flag"] = np.where(main["imputation_method"] == "original", 0, 1)
    t_recon += time.perf_counter() - _t_recon

    # ── reports + final frame ─────────────────────────────────────────────────
    audit_rows = []
    for col in CONT_VARS + RAIN_VARS:
        if col in main.columns:
            audit_rows.append({
                "variable": col,
                "original": int((main["imputation_method"] == "original").sum()),
                "interpolation": int((main["imputation_method"] == "interpolation").sum()),
                "model": int((main["imputation_method"] == "model").sum()),
                "neighbor": int((main["imputation_method"] == "neighbor").sum()),
                "still_missing": int(main[col].isna().sum()),
            })

    spatial_drop = []
    for nid in NEIGHBOR_IDS:
        spatial_drop += [c for c in main.columns if c.endswith(f"_{nid}")]
    final_df = main.drop(columns=spatial_drop)
    tracking = ["gap_type", "gap_id", "gap_size", "filled_flag",
                "imputation_method", "confidence_level"]
    other = [c for c in final_df.columns if c not in tracking]
    final_df = final_df[other + tracking]

    return {
        "final_df": final_df,
        "interval_min": INTERVAL_MIN,
        "reports": {
            "audit_report": pd.DataFrame(audit_rows),
            "model_selection_report": pd.DataFrame(selection_rows),
            "neighbor_quality_report": pd.DataFrame(neighbor_quality),
        },
        # Phase timings (seconds) for performance logging (Improvement 3).
        "timings": {
            "feature_eng_s": round(t_feature_eng[0], 2),
            "model_training_s": round(t_model_train, 2),
            "reconstruction_s": round(t_recon, 2),
            "trained_models": bool(has_medium_gaps and model_selection),
        },
    }
