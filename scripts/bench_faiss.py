"""faiss IndexFlatIP 内存搜基准测试。

模拟生产规模（170k × 1024 维）；bge-m3 输出已归一化，内积 = 余弦相似度。
"""
from __future__ import annotations

import time

import faiss
import numpy as np

DIM = 1024
N = 170_000
QUERIES = 100

print(f"[setup] generating N={N:,} × dim={DIM} normalized vectors …")
rng = np.random.default_rng(42)
t0 = time.time()
arr = rng.standard_normal((N, DIM)).astype("float32")
arr /= np.linalg.norm(arr, axis=1, keepdims=True)
print(f"  generated in {time.time()-t0:.1f}s, mem={arr.nbytes/1024/1024:.0f}MB")

t0 = time.time()
index = faiss.IndexFlatIP(DIM)
index.add(arr)
print(f"[setup] faiss IndexFlatIP add() took {time.time()-t0:.2f}s, ntotal={index.ntotal}")

# 单线程 vs 默认多线程
import os
threads_default = faiss.omp_get_max_threads()
print(f"[setup] faiss OMP threads default = {threads_default}")

qrng = np.random.default_rng(1)
queries = qrng.standard_normal((QUERIES, DIM)).astype("float32")
queries /= np.linalg.norm(queries, axis=1, keepdims=True)

for K in (5, 20, 50):
    # warm
    index.search(queries[:1], K)
    lat = []
    for q in queries:
        t = time.time()
        index.search(q.reshape(1, -1), K)
        lat.append((time.time() - t) * 1000)
    lat.sort()
    print(
        f"[knn k={K:2d}]  median={lat[len(lat)//2]:.2f}ms  "
        f"p90={lat[int(len(lat)*0.9)]:.2f}ms  "
        f"max={lat[-1]:.2f}ms  "
        f"avg={sum(lat)/len(lat):.2f}ms  ({len(lat)} runs)"
    )

# 单线程对比（demo 单核场景）
faiss.omp_set_num_threads(1)
print("\n--- single thread (worst case) ---")
for K in (5, 20):
    lat = []
    for q in queries:
        t = time.time()
        index.search(q.reshape(1, -1), K)
        lat.append((time.time() - t) * 1000)
    lat.sort()
    print(
        f"[knn k={K:2d} 1-thr]  median={lat[len(lat)//2]:.2f}ms  "
        f"p90={lat[int(len(lat)*0.9)]:.2f}ms  avg={sum(lat)/len(lat):.2f}ms"
    )
