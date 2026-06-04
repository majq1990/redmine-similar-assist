"""sqlite-vec 性能基准测试。

造一个跟生产规模相当的临时库（170k × 1024 维），跑 KNN 看延迟。
"""
from __future__ import annotations

import os
import random
import sqlite3
import struct
import time
from pathlib import Path

import numpy as np
import sqlite_vec

DB = Path("data/bench.db")
DIM = 1024
N = 170_000
QUERIES = 20

if DB.exists():
    DB.unlink()


def pack(v: np.ndarray) -> bytes:
    return struct.pack(f"{DIM}f", *v.astype("float32"))


print(f"[setup] building bench db at {DB} with N={N:,} × dim={DIM} …")
conn = sqlite3.connect(str(DB))
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
conn.execute(f"CREATE VIRTUAL TABLE vec USING vec0(embedding FLOAT[{DIM}])")

rng = np.random.default_rng(42)
t0 = time.time()
BATCH = 5000
for start in range(0, N, BATCH):
    rows = []
    cnt = min(BATCH, N - start)
    arr = rng.standard_normal((cnt, DIM)).astype("float32")
    # L2 normalize（模拟 bge-m3 归一化向量）
    arr = arr / np.linalg.norm(arr, axis=1, keepdims=True)
    for i, vec in enumerate(arr):
        rows.append((start + i + 1, pack(vec)))
    conn.executemany("INSERT INTO vec(rowid, embedding) VALUES (?, ?)", rows)
    if (start + BATCH) % 50000 == 0 or start + BATCH >= N:
        print(f"  inserted {start+cnt:,} / {N:,}  ({time.time()-t0:.1f}s)")
conn.commit()
t_insert = time.time() - t0
db_size = DB.stat().st_size / (1024 * 1024)
print(f"[setup] done. insert took {t_insert:.1f}s, file={db_size:.1f} MB")

# 准备 query 向量
qrng = np.random.default_rng(1)
queries = qrng.standard_normal((QUERIES, DIM)).astype("float32")
queries = queries / np.linalg.norm(queries, axis=1, keepdims=True)

# 跑 KNN，分别测 k=5/k=20
for K in (5, 20, 50):
    lat = []
    for q in queries:
        t = time.time()
        list(
            conn.execute(
                "SELECT rowid, distance FROM vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (pack(q), K),
            )
        )
        lat.append((time.time() - t) * 1000)
    lat_sorted = sorted(lat)
    print(
        f"[knn k={K:2d}]  median={lat_sorted[len(lat)//2]:.1f}ms  "
        f"p90={lat_sorted[int(len(lat)*0.9)]:.1f}ms  "
        f"max={lat_sorted[-1]:.1f}ms  "
        f"avg={sum(lat)/len(lat):.1f}ms  ({len(lat)} runs)"
    )

print(f"\n[result] N={N:,} × dim={DIM}  db_file={db_size:.1f} MB")
