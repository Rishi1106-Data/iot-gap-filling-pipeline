#!/usr/bin/env python3
"""infrastructure/dynamodb/seed_sensors.py — populate annam-sensor-metadata.

Two modes:

  1. --from-coords coords.csv
     Bulk-seed from a CSV of (device_id, latitude, longitude). For each sensor it
     computes the K nearest other sensors by haversine distance and writes them
     as the 'neighbors' attribute. This is how you bootstrap topology from the
     Lat/Long columns already present in the raw sensor CSVs.

  2. --item '{...}'
     Upsert a single fully-specified metadata item (for manual edits).

Cost note: writing topology ONCE into DynamoDB means every pipeline run reads it
with a single GetItem — neighbours never need recomputation at run time.

Example coords.csv:
    device_id,latitude,longitude
    201,31.274033,74.849239
    237,31.40,74.95
    249,31.10,74.70
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from decimal import Decimal
from typing import Dict, List, Tuple

import boto3


def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def compute_neighbors(
    coords: Dict[str, Tuple[float, float]], k: int, max_km: float
) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {}
    ids = list(coords)
    for tid in ids:
        dists = []
        for nid in ids:
            if nid == tid:
                continue
            d = haversine_km(coords[tid], coords[nid])
            if d <= max_km:
                dists.append((nid, round(d, 2)))
        dists.sort(key=lambda x: x[1])
        out[tid] = [{"id": nid, "distance_km": Decimal(str(d))} for nid, d in dists[:k]]
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--region", default="ap-south-1")
    p.add_argument("--table", default="annam-sensor-metadata")
    p.add_argument("--from-coords", help="CSV: device_id,latitude,longitude")
    p.add_argument("--item", help="Single JSON metadata item to upsert")
    p.add_argument("--k", type=int, default=2, help="neighbours per sensor")
    p.add_argument("--max-km", type=float, default=30.0, help="max neighbour distance")
    args = p.parse_args()

    ddb = boto3.resource("dynamodb", region_name=args.region)
    table = ddb.Table(args.table)

    if args.item:
        item = json.loads(args.item, parse_float=Decimal)
        item.setdefault("PK", f"SENSOR#{item['device_id']}")
        item.setdefault("SK", "META")
        item.setdefault("status", "ACTIVE")
        table.put_item(Item=item)
        print(f"Upserted SENSOR#{item['device_id']}")
        return 0

    if not args.from_coords:
        p.error("provide --from-coords or --item")

    coords: Dict[str, Tuple[float, float]] = {}
    with open(args.from_coords) as f:
        for row in csv.DictReader(f):
            coords[str(row["device_id"])] = (float(row["latitude"]), float(row["longitude"]))

    neighbors = compute_neighbors(coords, args.k, args.max_km)

    written = 0
    with table.batch_writer() as batch:
        for did, (lat, lon) in coords.items():
            batch.put_item(
                Item={
                    "PK": f"SENSOR#{did}",
                    "SK": "META",
                    "device_id": did,
                    "status": "ACTIVE",
                    "latitude": Decimal(str(lat)),
                    "longitude": Decimal(str(lon)),
                    "ref_col": "CorrectedTemp",
                    "neighbors": neighbors.get(did, []),
                    "updated_at": "seed",
                }
            )
            written += 1
    print(f"Seeded {written} sensors with up to k={args.k} neighbours each.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
