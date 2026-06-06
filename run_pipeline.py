"""run_pipeline.py — CLI entry point for the IoT gap-filling pipeline.

Usage:
    python run_pipeline.py --config config/config.yaml
"""

import sys
import time
import argparse
import logging

import yaml

sys.path.insert(0, "src")
import main as pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the IoT gap-filling pipeline.")
    parser.add_argument("--config", required=True, help="Path to the YAML config file.")
    return parser.parse_args()


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main() -> int:
    args = parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"ERROR: config file not found: {args.config}")
        return 1
    except yaml.YAMLError as exc:
        print(f"ERROR: could not parse YAML config: {exc}")
        return 1

    start = time.time()
    try:
        filled_dataset, evaluation_report, execution_summary = pipeline.run_pipeline(config)
    except Exception as exc:
        elapsed = time.time() - start
        print("\n================ PIPELINE FAILED ================")
        print(f"status        : failed")
        print(f"error         : {exc}")
        print(f"execution time: {elapsed:.1f}s")
        return 1

    elapsed = time.time() - start
    print("\n================ PIPELINE SUMMARY ================")
    print(f"status        : {execution_summary.get('status')}")
    print(f"rows processed: {execution_summary.get('rows_total'):,}")
    print(f"method counts :")
    for method, count in execution_summary.get('method_counts', {}).items():
        print(f"    {method:<14}: {count:,}")
    print(f"execution time: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    logging.getLogger("iot_pipeline").setLevel(logging.INFO)
    sys.exit(main())
