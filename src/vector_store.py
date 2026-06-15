"""sqlite 持久层 + faiss 内存索引混合实现。

设计：
  - sqlite-vec 表 vec_issues：原始向量 BLOB 持久化（重启后从这里全量加载到 faiss）
  - sqlite 表 issues_meta：元数据 (subject/status/resolution/...)
  - faiss IndexIDMap(IndexFlatIP)：内存索引，KNN 走它。bge-m3 向量已归一化，
    内积 = 余弦相似度，distance = cos in [-1, 1]（越大越相似）

性能（170k×1024）：
  - faiss KNN p90 < 100ms（16 thread）/ 80ms（1 thread）
  - 内存常驻 ~680MB

启动：从 sqlite 全量 load 到 faiss（170k 大约 5-10 秒）。
upsert：sqlite + faiss 同步更新（remove + add）。
"""
from __future__ import annotations

import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

import faiss
import numpy as np
import sqlite_vec

from .config import cfg, project_root


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(buf: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(buf, dtype="float32", count=dim)


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v if n == 0 else (v / n).astype("float32")


class VectorStore:
    def __init__(self) -> None:
        c = cfg()
        path = project_root() / c["storage"]["vector_db"]
        path.parent.mkdir(parents=True, exist_ok=True)
        self.dim = c["embedding"]["dim"]
        # check_same_thread=False：webhook 启动时主线程创建 singleton，
        # 实际查询发生在 Flask worker 线程。WAL + busy_timeout 保证并发安全。
        self.conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
        # WAL 模式：允许并发读 + 单 writer（适合 backfill 和 sync 跨进程场景）
        # busy_timeout 30s：另一 writer 时等待而非立即报 "database is locked"
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下 NORMAL 安全且更快
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._ensure_schema()

        # faiss 内存索引：IndexFlatIP 走内积，bge-m3 归一化向量 -> 内积 = 余弦
        # IndexIDMap 让我们能直接用 issue_id 作为 KNN 的返回 id
        self._index = faiss.IndexIDMap(faiss.IndexFlatIP(self.dim))
        self._load_into_faiss()

    def _ensure_schema(self) -> None:
        c = self.conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS issues_meta(
                issue_id        INTEGER PRIMARY KEY,
                subject         TEXT,
                status          TEXT,
                closed_on       TEXT,
                resolution      TEXT,
                updated_on      TEXT,
                embed_text_hash TEXT
            )"""
        )
        # 老库平滑升级（v0 没 embed_text_hash 列）
        cols = {r[1] for r in c.execute("PRAGMA table_info(issues_meta)").fetchall()}
        if "embed_text_hash" not in cols:
            c.execute("ALTER TABLE issues_meta ADD COLUMN embed_text_hash TEXT")
        c.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_issues USING vec0("
            f"embedding FLOAT[{self.dim}])"
        )
        self.conn.commit()

    def _load_into_faiss(self) -> None:
        """启动时把全量向量从 sqlite-vec 加载到 faiss。"""
        t0 = time.time()
        cur = self.conn.execute("SELECT rowid, embedding FROM vec_issues")
        ids: list[int] = []
        vecs: list[np.ndarray] = []
        for rowid, blob in cur:
            ids.append(int(rowid))
            v = _unpack(blob, self.dim)
            vecs.append(_l2_normalize(v))
        if vecs:
            arr = np.stack(vecs).astype("float32")
            id_arr = np.array(ids, dtype="int64")
            self._index.add_with_ids(arr, id_arr)
        print(
            f"[vector_store] loaded {len(ids):,} vectors into faiss "
            f"in {time.time()-t0:.1f}s",
            flush=True,
        )

    def upsert(self, issue_id: int, embedding: list[float], meta: dict[str, Any]) -> None:
        self.upsert_many([(issue_id, embedding, meta)])

    def upsert_many(
        self, items: list[tuple[int, list[float], dict[str, Any]]]
    ) -> None:
        """批量持久化并更新 FAISS，只提交一次 SQLite 事务。"""
        if not items:
            return

        c = self.conn.cursor()
        for issue_id, embedding, meta in items:
            c.execute("DELETE FROM vec_issues WHERE rowid=?", (issue_id,))
            c.execute(
                "INSERT INTO vec_issues(rowid, embedding) VALUES(?, ?)",
                (issue_id, _pack(embedding)),
            )
            c.execute(
                """INSERT INTO issues_meta(issue_id, subject, status, closed_on, resolution, updated_on, embed_text_hash)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(issue_id) DO UPDATE SET
                     subject=excluded.subject,
                     status=excluded.status,
                     closed_on=excluded.closed_on,
                     resolution=excluded.resolution,
                     updated_on=excluded.updated_on,
                     embed_text_hash=excluded.embed_text_hash""",
                (
                    issue_id,
                    meta.get("subject"),
                    meta.get("status"),
                    meta.get("closed_on"),
                    meta.get("resolution"),
                    meta.get("updated_on"),
                    meta.get("embed_text_hash"),
                ),
            )
        self.conn.commit()

        # FAISS 同步也批量执行，避免逐条 remove/add。
        ids = np.array([item[0] for item in items], dtype="int64")
        vecs = np.stack(
            [
                _l2_normalize(np.asarray(item[1], dtype="float32"))
                for item in items
            ]
        ).astype("float32")
        try:
            self._index.remove_ids(ids)
        except Exception:
            pass
        self._index.add_with_ids(vecs, ids)

    def has(self, issue_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM issues_meta WHERE issue_id=?", (issue_id,)
        ).fetchone()
        return row is not None

    def get_meta(self, issue_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT subject, status, closed_on, resolution, updated_on, embed_text_hash "
            "FROM issues_meta WHERE issue_id=?",
            (issue_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "subject": row[0],
            "status": row[1],
            "closed_on": row[2],
            "resolution": row[3],
            "updated_on": row[4],
            "embed_text_hash": row[5],
        }

    def update_meta_only(self, issue_id: int, meta: dict) -> None:
        """便宜路径：仅更新 issues_meta，不动 vec_issues / faiss。"""
        self.conn.execute(
            """UPDATE issues_meta SET
                  status=COALESCE(?, status),
                  closed_on=COALESCE(?, closed_on),
                  resolution=COALESCE(?, resolution),
                  updated_on=COALESCE(?, updated_on)
                WHERE issue_id=?""",
            (
                meta.get("status"),
                meta.get("closed_on"),
                meta.get("resolution"),
                meta.get("updated_on"),
                issue_id,
            ),
        )
        self.conn.commit()

    def knn(
        self, embedding: list[float], top: int, exclude_id: int | None = None
    ) -> list[dict]:
        if self._index.ntotal == 0:
            return []
        q = _l2_normalize(np.asarray(embedding, dtype="float32")).reshape(1, -1)
        k = top + (1 if exclude_id else 0)
        D, I = self._index.search(q, k)  # D: cos sim, I: issue_id
        out: list[dict] = []
        # 一次性把候选 meta 拿出来，避免 N+1
        ids = [int(i) for i in I[0] if i != -1 and (not exclude_id or int(i) != exclude_id)]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT issue_id, subject, status, closed_on, resolution "
            f"FROM issues_meta WHERE issue_id IN ({placeholders})",
            ids,
        ).fetchall()
        meta_by_id = {r[0]: r for r in rows}
        # 按 faiss 返回顺序输出
        seen = 0
        for cos_sim, iid in zip(D[0], I[0]):
            iid = int(iid)
            if iid == -1:
                continue
            if exclude_id and iid == exclude_id:
                continue
            m = meta_by_id.get(iid)
            if not m:
                continue
            _, subject, status, closed_on, resolution = m
            cosine = float(cos_sim)  # 已是余弦
            out.append(
                {
                    "issue_id": iid,
                    "distance": 1.0 - cosine,  # 兼容旧字段
                    "cosine": cosine,
                    "subject": subject,
                    "status": status,
                    "closed_on": closed_on,
                    "resolution": resolution,
                }
            )
            seen += 1
            if seen >= top:
                break
        return out


_singleton: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """进程级单例。首次调用 load 全量 faiss（168k 约 30 秒），之后复用。

    webhook_server 启动时调一次预热，避免首个真实请求被卡。
    """
    global _singleton
    if _singleton is None:
        _singleton = VectorStore()
    return _singleton


# === 钉钉知识库文档独立索引 ===

import hashlib as _hashlib


def _doc_id_to_int64(node_id: str) -> int:
    """钉钉 nodeId 是字符串，faiss IndexIDMap 要 int64。
    取 sha1 前 8 字节转 int63，碰撞概率 ~10^-19，几百到几千文档量级完全无忧。
    """
    h = _hashlib.sha1(node_id.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") & 0x7FFFFFFFFFFFFFFF


class DocStore:
    """钉钉知识库文档向量库 + faiss 索引。独立于 VectorStore（issues）。"""

    def __init__(self) -> None:
        c = cfg()
        path = project_root() / c["storage"]["vector_db"]  # 跟 issues 共用一个 sqlite 文件
        self.dim = c["embedding"]["dim"]
        self.conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._ensure_schema()
        self._index = faiss.IndexIDMap(faiss.IndexFlatIP(self.dim))
        self._load_into_faiss()

    def _ensure_schema(self) -> None:
        c = self.conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS docs_meta(
                node_id         TEXT PRIMARY KEY,
                workspace_id    TEXT,
                title           TEXT,
                url             TEXT,
                summary         TEXT,
                update_time     INTEGER,
                embed_text_hash TEXT,
                synced_at       TEXT
            )"""
        )
        c.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_docs USING vec0("
            f"embedding FLOAT[{self.dim}])"
        )
        self.conn.commit()

    def _load_into_faiss(self) -> None:
        t0 = time.time()
        cur = self.conn.execute("SELECT rowid, embedding FROM vec_docs")
        ids: list[int] = []
        vecs: list[np.ndarray] = []
        for rowid, blob in cur:
            ids.append(int(rowid))
            vecs.append(_l2_normalize(_unpack(blob, self.dim)))
        if vecs:
            self._index.add_with_ids(
                np.stack(vecs).astype("float32"),
                np.array(ids, dtype="int64"),
            )
        import sys as _sys
        _sys.stderr.write(
            f"[doc_store] loaded {len(ids)} docs into faiss in {time.time()-t0:.1f}s\n"
        )

    def has(self, node_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM docs_meta WHERE node_id=?", (node_id,)
        ).fetchone()
        return row is not None

    def get_meta(self, node_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT workspace_id, title, url, summary, update_time, embed_text_hash "
            "FROM docs_meta WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "workspace_id": row[0], "title": row[1], "url": row[2],
            "summary": row[3], "update_time": row[4], "embed_text_hash": row[5],
        }

    def upsert(self, node_id: str, embedding: list[float], meta: dict[str, Any]) -> None:
        rowid = _doc_id_to_int64(node_id)
        c = self.conn.cursor()
        c.execute("DELETE FROM vec_docs WHERE rowid=?", (rowid,))
        c.execute(
            "INSERT INTO vec_docs(rowid, embedding) VALUES(?, ?)",
            (rowid, _pack(embedding)),
        )
        c.execute(
            """INSERT INTO docs_meta(node_id, workspace_id, title, url, summary, update_time, embed_text_hash, synced_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(node_id) DO UPDATE SET
                 workspace_id=excluded.workspace_id,
                 title=excluded.title,
                 url=excluded.url,
                 summary=excluded.summary,
                 update_time=excluded.update_time,
                 embed_text_hash=excluded.embed_text_hash,
                 synced_at=excluded.synced_at""",
            (
                node_id,
                meta.get("workspace_id"),
                meta.get("title"),
                meta.get("url"),
                meta.get("summary"),
                meta.get("update_time"),
                meta.get("embed_text_hash"),
                meta.get("synced_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
            ),
        )
        self.conn.commit()
        vec = _l2_normalize(np.asarray(embedding, dtype="float32"))
        try:
            self._index.remove_ids(np.array([rowid], dtype="int64"))
        except Exception:
            pass
        self._index.add_with_ids(vec.reshape(1, -1), np.array([rowid], dtype="int64"))

    def knn(self, embedding: list[float], top: int) -> list[dict]:
        if self._index.ntotal == 0:
            return []
        q = _l2_normalize(np.asarray(embedding, dtype="float32")).reshape(1, -1)
        D, I = self._index.search(q, top)
        out: list[dict] = []
        rowids = [int(x) for x in I[0] if x != -1]
        if not rowids:
            return []
        # rowid → node_id 反查
        placeholders = ",".join("?" * len(rowids))
        # 用 sha1[:8]/int63 hash 推不出 node_id，需要查 docs_meta 全表的 hash 映射
        # 简化：扫一遍 docs_meta，建 rowid→meta 映射
        rows = self.conn.execute(
            "SELECT node_id, title, url, summary FROM docs_meta"
        ).fetchall()
        node_by_rowid = {_doc_id_to_int64(r[0]): r for r in rows}
        for cos_sim, rowid in zip(D[0], I[0]):
            if rowid == -1:
                continue
            meta = node_by_rowid.get(int(rowid))
            if not meta:
                continue
            out.append(
                {
                    "node_id": meta[0],
                    "title": meta[1],
                    "url": meta[2],
                    "summary": meta[3],
                    "cosine": float(cos_sim),
                }
            )
            if len(out) >= top:
                break
        return out


_doc_singleton: DocStore | None = None


def get_doc_store() -> DocStore:
    global _doc_singleton
    if _doc_singleton is None:
        _doc_singleton = DocStore()
    return _doc_singleton


# === 钉钉知识库文档"chunk 粒度"索引（B1 分块嵌入） ===


def _chunk_id_to_int64(node_id: str, chunk_idx: int) -> int:
    """chunk 级 rowid：sha1(node_id + ":" + chunk_idx)[:8] → int63。

    碰撞率：node_id 字符串空间 + idx 数字 ≤ 数十万 chunks 量级，sha1[:64bit] 仍安全。
    """
    h = _hashlib.sha1(f"{node_id}:{chunk_idx}".encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") & 0x7FFFFFFFFFFFFFFF


class ChunkStore:
    """钉钉知识库文档 **chunk 粒度** 向量库 + faiss。

    与 DocStore（整篇 embed）并存：
      - DocStore 仍保留作为回退/兼容
      - ChunkStore 是 B1 新主路径：每篇 wiki 切成多段、每段独立 embed，KNN 精度更高
    """

    def __init__(self) -> None:
        c = cfg()
        path = project_root() / c["storage"]["vector_db"]
        self.dim = c["embedding"]["dim"]
        self.conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._ensure_schema()
        self._index = faiss.IndexIDMap(faiss.IndexFlatIP(self.dim))
        # rowid → (node_id, chunk_idx) 反查表（faiss 只返 int64 rowid）
        self._rowid_map: dict[int, tuple[str, int]] = {}
        self._load_into_faiss()

    def _ensure_schema(self) -> None:
        c = self.conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS doc_chunks_meta(
                node_id     TEXT NOT NULL,
                chunk_idx   INTEGER NOT NULL,
                heading     TEXT,
                text        TEXT,
                embed_hash  TEXT,
                synced_at   TEXT,
                PRIMARY KEY(node_id, chunk_idx)
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_node ON doc_chunks_meta(node_id)"
        )
        c.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_doc_chunks USING vec0("
            f"embedding FLOAT[{self.dim}])"
        )
        self.conn.commit()

    def _load_into_faiss(self) -> None:
        t0 = time.time()
        # 拼 rowid → (node_id, chunk_idx) 反查；遍历 doc_chunks_meta 算 rowid
        # 同时从 vec_doc_chunks 拿向量
        cur = self.conn.execute(
            "SELECT node_id, chunk_idx FROM doc_chunks_meta"
        )
        for nid, idx in cur:
            rowid = _chunk_id_to_int64(nid, idx)
            self._rowid_map[rowid] = (nid, idx)

        ids: list[int] = []
        vecs: list[np.ndarray] = []
        for rowid, blob in self.conn.execute(
            "SELECT rowid, embedding FROM vec_doc_chunks"
        ):
            ids.append(int(rowid))
            vecs.append(_l2_normalize(_unpack(blob, self.dim)))
        if vecs:
            self._index.add_with_ids(
                np.stack(vecs).astype("float32"),
                np.array(ids, dtype="int64"),
            )
        import sys as _sys
        _sys.stderr.write(
            f"[chunk_store] loaded {len(ids)} chunks into faiss in "
            f"{time.time()-t0:.1f}s\n"
        )

    def get_chunks_hash(self, node_id: str) -> list[tuple[int, str]]:
        """返回 [(chunk_idx, embed_hash)]，用于增量判断该文档是否需要重切重 embed。"""
        return list(
            self.conn.execute(
                "SELECT chunk_idx, embed_hash FROM doc_chunks_meta "
                "WHERE node_id=? ORDER BY chunk_idx",
                (node_id,),
            )
        )

    def delete_doc(self, node_id: str) -> None:
        """删一篇文档的所有 chunks（重切前先调，避免 idx 越界残留）。"""
        # 拿到该 doc 全部 rowid 用于 faiss 删除
        old_idxs = [
            r[0]
            for r in self.conn.execute(
                "SELECT chunk_idx FROM doc_chunks_meta WHERE node_id=?",
                (node_id,),
            )
        ]
        if not old_idxs:
            return
        rowids = np.array(
            [_chunk_id_to_int64(node_id, i) for i in old_idxs],
            dtype="int64",
        )
        c = self.conn.cursor()
        c.execute("DELETE FROM doc_chunks_meta WHERE node_id=?", (node_id,))
        for rid in rowids.tolist():
            c.execute("DELETE FROM vec_doc_chunks WHERE rowid=?", (rid,))
        self.conn.commit()
        try:
            self._index.remove_ids(rowids)
        except Exception:
            pass
        for rid in rowids.tolist():
            self._rowid_map.pop(int(rid), None)

    def upsert_many(
        self,
        items: list[tuple[str, int, list[float], str, str, str]],
    ) -> None:
        """批量入库 chunks。

        items: [(node_id, chunk_idx, embedding, heading, text, embed_hash)]
        调用方应在覆盖前先 delete_doc(node_id) 清旧 chunks，避免残留。
        """
        if not items:
            return
        c = self.conn.cursor()
        rowids: list[int] = []
        vecs: list[np.ndarray] = []
        synced_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        for node_id, idx, emb, heading, text, ehash in items:
            rowid = _chunk_id_to_int64(node_id, idx)
            rowids.append(rowid)
            vecs.append(_l2_normalize(np.asarray(emb, dtype="float32")))
            c.execute("DELETE FROM vec_doc_chunks WHERE rowid=?", (rowid,))
            c.execute(
                "INSERT INTO vec_doc_chunks(rowid, embedding) VALUES(?, ?)",
                (rowid, _pack(emb)),
            )
            c.execute(
                """INSERT INTO doc_chunks_meta(
                       node_id, chunk_idx, heading, text, embed_hash, synced_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(node_id, chunk_idx) DO UPDATE SET
                       heading=excluded.heading,
                       text=excluded.text,
                       embed_hash=excluded.embed_hash,
                       synced_at=excluded.synced_at""",
                (node_id, idx, heading, text, ehash, synced_at),
            )
            self._rowid_map[rowid] = (node_id, idx)
        self.conn.commit()
        # FAISS 批量 remove + add
        ids_arr = np.array(rowids, dtype="int64")
        try:
            self._index.remove_ids(ids_arr)
        except Exception:
            pass
        self._index.add_with_ids(
            np.stack(vecs).astype("float32"), ids_arr
        )

    def get_doc_chunks(self, node_id: str) -> list[dict]:
        """返回该文档全部 chunks（按 idx 升序），用于 get_chunk_with_context。"""
        rows = self.conn.execute(
            "SELECT chunk_idx, heading, text FROM doc_chunks_meta "
            "WHERE node_id=? ORDER BY chunk_idx",
            (node_id,),
        ).fetchall()
        return [
            {"idx": r[0], "heading": r[1] or "", "text": r[2] or ""}
            for r in rows
        ]

    def knn(self, embedding: list[float], top: int) -> list[dict]:
        """chunk 粒度 KNN。返回 [{node_id, chunk_idx, heading, text, cosine}]。

        Pipeline 拿到后应在外层做"按 node_id 聚合 → 每 doc 取最高分 chunk"。
        """
        if self._index.ntotal == 0:
            return []
        q = _l2_normalize(np.asarray(embedding, dtype="float32")).reshape(1, -1)
        D, I = self._index.search(q, top)
        # 拿 (node_id, chunk_idx) 列表，一次性 SELECT 拼 meta
        hits: list[tuple[float, str, int]] = []
        for cos_sim, rowid in zip(D[0], I[0]):
            if rowid == -1:
                continue
            key = self._rowid_map.get(int(rowid))
            if not key:
                continue
            hits.append((float(cos_sim), key[0], key[1]))
        if not hits:
            return []
        # 批量取 heading/text
        # 用 (node_id, chunk_idx) 元组列表反查不方便，分组按 node_id 查
        by_nid: dict[str, dict] = {}
        for _, nid, _idx in hits:
            by_nid.setdefault(nid, {})
        placeholders = ",".join("?" * len(by_nid))
        for nid, idx, h, t in self.conn.execute(
            f"SELECT node_id, chunk_idx, heading, text FROM doc_chunks_meta "
            f"WHERE node_id IN ({placeholders})",
            list(by_nid.keys()),
        ):
            by_nid[nid][idx] = (h or "", t or "")
        out: list[dict] = []
        for cos, nid, idx in hits:
            meta = by_nid.get(nid, {}).get(idx)
            if not meta:
                continue
            out.append(
                {
                    "node_id": nid,
                    "chunk_idx": idx,
                    "heading": meta[0],
                    "text": meta[1],
                    "cosine": cos,
                }
            )
        return out

    def count(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) FROM doc_chunks_meta").fetchone()[0]
        )

    def count_docs(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(DISTINCT node_id) FROM doc_chunks_meta"
            ).fetchone()[0]
        )


_chunk_singleton: ChunkStore | None = None


def get_chunk_store() -> ChunkStore:
    global _chunk_singleton
    if _chunk_singleton is None:
        _chunk_singleton = ChunkStore()
    return _chunk_singleton
