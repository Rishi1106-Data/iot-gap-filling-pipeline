# =============================================================================
# aws/Dockerfile — IoT Gap-Filling Pipeline (AWS Fargate image)
# =============================================================================
# Build context = REPO ROOT (so src/, run_pipeline.py, and aws/ are all copied).
#   docker build -f aws/Dockerfile -t annam-gapfill .
#
# Critical invariant discovered by inspecting the code:
#   • run_pipeline.py does  sys.path.insert(0, "src"); import main
#   • src/main.py uses      from src import gap_analysis as ga
#   • src/neighbor_reconstruction.py uses  from gap_analysis import parse_timestamps
#   Both import styles only resolve when the process CWD is the REPO ROOT and
#   'src' is on sys.path. Therefore WORKDIR must be the repo root (/app) and
#   aws/run_batch.py replicates the exact sys.path setup. Do not change WORKDIR.
# =============================================================================

# ---- Stage 1: builder -------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# libgomp1 is needed by xgboost/lightgbm at RUNTIME; build-essential only here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels

# Pipeline deps (pinned in requirements.txt) + the AWS layer deps. boto3 is the
# only addition; everything else already matches the tested pipeline.
COPY requirements.txt ./requirements.txt
COPY aws/requirements-aws.txt ./requirements-aws.txt

RUN pip install --upgrade pip wheel \
 && pip wheel --wheel-dir /wheels -r requirements.txt -r requirements-aws.txt

# ---- Stage 2: runtime -------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    PIP_NO_CACHE_DIR=1

# Runtime-only native lib for the gradient-boosting wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (Fargate best practice).
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Install pre-built wheels (fast, reproducible, no compiler in final image).
COPY --from=builder /wheels /wheels
COPY requirements.txt aws/requirements-aws.txt ./
RUN pip install --no-index --find-links=/wheels \
        -r requirements.txt -r aws/requirements-aws.txt \
 && rm -rf /wheels

# Copy the application. src/ stays byte-for-byte identical to the repo.
COPY src/ ./src/
COPY run_pipeline.py ./run_pipeline.py
COPY aws/ ./aws/

# Scratch + cache dirs (ephemeral; Fargate task storage). Owned by appuser.
RUN mkdir -p /tmp/iot && chown -R appuser:appuser /app /tmp/iot
USER appuser

# WORKDIR is /app (the repo root) — REQUIRED for the import resolution above.
# Default command processes a batch; Step Functions overrides the args.
ENTRYPOINT ["python", "aws/run_batch.py"]
CMD ["--help"]
