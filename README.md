# IoT Weather Sensor Gap-Filling Pipeline

A reusable, configuration-driven pipeline for reconstructing missing values in IoT
weather-sensor time series. It routes each gap to the most appropriate method based on
gap length, uses a model tournament to select the best regressor per variable, and
reconstructs long outages from nearby sensors. The pipeline is designed to be applied
to any device by editing a single YAML file — no code changes required.

---

## 1. Project Overview

IoT weather stations report on a fixed cadence (nominally every 5 minutes) but, in
practice, drop readings due to power, connectivity, or hardware issues. Downstream
analytics need a complete, regularly-spaced series.

This pipeline:

- Aligns raw transmissions to a fixed 5-minute grid and detects every missing slot.
- Classifies gaps by length and reconstructs each with a method suited to its size.
- Tracks the provenance of every value (original, interpolated, model, or neighbour)
  with an explicit confidence level, so reconstructed data is never silently mixed with
  observed data.
- Evaluates its own accuracy honestly using synthetic gap masking on a chronological
  hold-out, reporting MAE, RMSE, R², and Bias per variable and gap size.

It supports two execution modes — **training** (run the model tournament and persist the
winners) and **inference** (load saved models and reconstruct deterministically).

---

## 2. Architecture

```
iot_gap_filling_pipeline/
├── run_pipeline.py              CLI entry point (argparse + YAML load)
├── requirements.txt             Pinned dependencies
├── README.md
│
├── config/
│   └── config.yaml              All paths, thresholds, and tunables
│
└── src/
    ├── gap_analysis.py          Load, clean, build timeline, classify gaps
    ├── interpolation.py         Tracking columns + isolated-gap interpolation
    ├── model_selection.py       Feature engineering, model tournament, medium-gap fill
    ├── neighbor_reconstruction.py  Neighbour loading, reliability, long-gap fill
    ├── validation.py            Rainfall, continuity, audit sync, evaluation
    └── main.py                  Orchestrator (no business logic)
```

Each module owns one stage and exposes plain functions. `main.py` wires them together in
order, holds the shared state, and writes outputs; it contains no reconstruction logic.

---

## 3. Workflow

```
Dataset
   ↓
Gap Analysis            (align to 5-min grid, classify isolated / medium / long)
   ↓
Isolated Gaps  ->  Interpolation          (strict time interpolation, 1 row)
   ↓
Medium Gaps    ->  ML Models              (best-of-tournament, iterative, 2–5 rows)
   ↓
Long Gaps      ->  Neighbor Reconstruction (weighted, boundary-anchored, >5 rows)
   ↓
Validation              (conservative rainfall, continuity checks, audit sync)
   ↓
Outputs                 (filled dataset + four reports)
```

Gap routing rules:

| Gap length        | Classification | Method                              | Confidence |
|-------------------|----------------|-------------------------------------|------------|
| 1 row             | isolated       | time interpolation                  | High       |
| 2–5 rows          | medium         | selected ML model (iterative)       | Medium     |
| more than 5 rows  | long           | neighbour temporal reconstruction   | Low–Medium |
| insufficient data | long           | left as NaN (never fabricated)      | —          |

---

## 4. Model Strategy

For each continuous variable, a tournament evaluates candidate models using
`TimeSeriesSplit` (chronological cross-validation, no random shuffling) and selects the
one with the lowest mean MAE. Candidates: XGBoost, LightGBM, RandomForest, and KNN.

A representative selection (recorded in `config.yaml` under `model_mapping`):

```
CorrectedTemp      -> XGBoost
CorrectedHumidity  -> LightGBM
AtmPressure        -> LightGBM
WindSpeed          -> RandomForest
```

The winning model per variable is data-dependent and re-determined whenever the pipeline
is run in training mode. Selection never uses AIC, and features are built with `shift(1)`
lags and rolling statistics so no future information leaks into a prediction.

---

## 5. Training Mode

Set in `config.yaml`:

```yaml
execution:
  train_mode: true
```

In training mode the pipeline:

1. Builds features and runs the model tournament per variable.
2. Selects and fits the best model on all available training rows.
3. Persists the fitted models to `models/model_selection.joblib`.
4. Writes a human-readable `models/model_mapping_report.txt` (`variable -> model`).

Use this whenever the device, season, or data characteristics change and the models
should be refreshed.

---

## 6. Inference Mode

```yaml
execution:
  train_mode: false
```

In inference mode the pipeline loads the previously saved models and skips the tournament
and all retraining. Reconstruction is deterministic and considerably faster, which makes
this the mode for scheduled/production runs once models are established.

---

## 7. Example Execution

Install dependencies and run against the configured device:

```bash
pip install -r requirements.txt

python run_pipeline.py --config config/config.yaml
```

To apply the pipeline to a different device, edit only the relevant sections of
`config/config.yaml`:

```yaml
input_paths:
  target_file: data/Annam_201.csv
  neighbor_files:
    - data/Annam_237_201.csv
    - data/Annam_249_201.csv

neighbor_distances:
  ids:          ["237", "249"]
  distances_km: [16.75, 17.5]
```

On completion the CLI prints a summary:

```
================ PIPELINE SUMMARY ================
status        : success
rows processed: 17,856
method counts :
    original      : 17,263
    interpolation : 521
    neighbor      : 39
    model         : 33
execution time: 102.0s
```

---

## 8. Output Reports

All outputs are written to the directory configured under `output_paths`:

| File                            | Contents                                                        |
|---------------------------------|-----------------------------------------------------------------|
| `filled_dataset.csv`            | Reconstructed series with audit columns (`TimeStamp` as a column) |
| `audit_report.csv`              | Per-variable counts by method and remaining missing rows        |
| `evaluation_report.csv`         | MAE / RMSE / R² / Bias per variable and synthetic gap size      |
| `neighbor_quality_report.csv`   | Coverage, missing %, and gap structure per neighbour            |
| `model_selection_report.csv`    | Tournament scores and the winning model per variable (train mode) |
| `mae_vs_gapsize.png`            | MAE vs gap size, per variable                                   |

Each row of `filled_dataset.csv` carries six audit columns: `gap_type`, `gap_id`,
`gap_size`, `filled_flag`, `imputation_method`, and `confidence_level`.

---

## 9. AWS Deployment Notes

The pipeline is a self-contained Python project with a single CLI entry point, which maps
cleanly onto several AWS patterns:

- **Storage.** Keep raw device and neighbour CSVs in S3; sync the input prefix locally (or
  mount via S3FS) and point `input_paths` at it. Write `output_paths` to a separate S3
  prefix.
- **Scheduled inference.** Run `train_mode: false` on a container (ECS/Fargate task or a
  Batch job) triggered by EventBridge on a schedule. Persisted models in
  `models/model_selection.joblib` should be stored in S3 and pulled in at start-up; the
  declarative `model_mapping` in `config.yaml` keeps the chosen models transparent without
  inspecting code.
- **Periodic retraining.** Run `train_mode: true` as a separate, less-frequent job that
  refreshes and re-uploads the model artifact.
- **Configuration.** `config.yaml` can live in S3, SSM Parameter Store, or be baked into
  the image. The CLI exit codes (0 success, 1 failure) integrate directly with Batch/Step
  Functions retry and alerting.
- **Resourcing.** The work is CPU-bound and single-node; the tournament is the heaviest
  step, so size training jobs with more vCPUs and keep inference jobs lightweight.

---

## 10. Future Improvements

- **Feature scaling for KNN.** KNN currently competes without scaling and is rarely
  selected; adding a scaler within a pipeline wrapper would make it a fairer candidate.
- **Configurable model grid.** Expose candidate models and hyperparameter ranges fully via
  config to support light tuning without code edits.
- **More neighbours and smarter weighting.** Support additional neighbours and richer
  reliability signals (e.g. elevation, microclimate similarity) for long-gap reconstruction.
- **Parallelism.** The per-variable tournament is independent across variables and can be
  parallelised to cut training time.
- **Automated model-drift checks.** Compare live evaluation metrics against a baseline and
  flag when retraining is warranted.
- **Packaging and tests.** Add a unit-test suite per module and package the project for
  `pip install` to streamline deployment.
