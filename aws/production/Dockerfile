# Dockerfile — Annam reconstruction worker (ONE task reconstructs ALL sensors)
#
# A single Fargate task runs run_batch.py, which loops over every sensor in one
# process. The image pull and the heavy library imports (xgboost / lightgbm /
# pyarrow) are therefore paid once per weekly run instead of ~143 times.
#
# ARCHITECTURE NOTE on platform:
#   This image builds for whatever architecture you build it on. The Terraform
#   task definition pins cpu_architecture = "X86_64" by default, which matches a
#   plain `docker build` on an x86 machine. If you switch Terraform to "ARM64"
#   for the ~20% cost saving, you MUST build this image for arm64:
#     docker buildx build --platform linux/arm64 -t <repo>:latest --push .
#   Building x86 while Terraform expects ARM64 (or vice-versa) makes the task
#   fail to start with "exec format error".

FROM python:3.11-slim

# System lib needed by lightgbm / xgboost at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so the layer caches when only source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code. run_sensor.py is kept for local single-sensor debugging.
COPY config.py io_dynamo.py reconstruct.py run_sensor.py list_sensors.py run_batch.py ./

# The scheduled Fargate task runs the whole batch in one process.
ENTRYPOINT ["python", "run_batch.py"]
