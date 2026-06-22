"""B1-6 端到端验证：504431 案件应召回「车载对接Wiki」。

直接走 chunk 模式 KNN，看「车载对接Wiki」是否进入 docs_knn_top=15。
然后再调 judge_docs 看 LLM 是否判 related。
"""
import sys, os
sys.path.insert(0, "/app")
os.chdir("/app")

from src.embedder import Embedder
from src.text_cleaner import build_issue_text, get_chunk_with_context
from src.vector_store import get_chunk_store, get_doc_store
from src.llm_judge import judge_docs

CARWIKI_NID = "AR4GpnMqJzML1Xr9saRkbzPBVKe0xjE3"
CARWIKI_TITLE = "车载对接Wiki"

# #504431 真实文本
subject = "【朝阳运管服】20260611朝阳市运管服车辆轨迹对接支持"
desc = (
    "朝阳市目前更换政务网系统。由于政务网环境和互联网环境隔离，"
    "车辆对接的轨迹和视频都通过互联网传输。目前移动提供了互联网服务器和互联网映射ip。"
    "互联网服务器和政务网服务器可以通信。现场和设备厂商借了一台设备，"
    "设置推送互联网映射端口，但轨迹和视频无法观看。请支持。"
)
text = build_issue_text(subject, desc)
print(f"[504431 文本长度] {len(text)}")

emb = Embedder().embed([text])[0]

cs = get_chunk_store()
ds = get_doc_store()
print(f"[ChunkStore] {cs.count()} chunks / {cs.count_docs()} docs loaded")

# === chunk 粒度 KNN top 60 ===
raw_hits = cs.knn(emb, top=45)
print(f"\n=== 原始 chunk hits top 45 ===")
carwiki_chunk_hits = []
for h in raw_hits[:20]:
    mark = "  <== CARWIKI" if h["node_id"] == CARWIKI_NID else ""
    print(f"  cos={h['cosine']:.3f} idx={h['chunk_idx']:2d} "
          f"node={h['node_id'][:12]} heading={h['heading'][:35]!r}{mark}")
    if h["node_id"] == CARWIKI_NID:
        carwiki_chunk_hits.append(h)

# 找全部 carwiki chunk 命中
for i, h in enumerate(raw_hits):
    if h["node_id"] == CARWIKI_NID and i >= 20:
        carwiki_chunk_hits.append(h)
print(f"\n车载对接Wiki 在 chunk top45 中命中数: {len(carwiki_chunk_hits)}")
if carwiki_chunk_hits:
    best = max(carwiki_chunk_hits, key=lambda x: x["cosine"])
    print(f"  最高 cos = {best['cosine']:.4f} (chunk idx={best['chunk_idx']})")

# === 按 node_id 聚合 ===
best_by_nid = {}
for h in raw_hits:
    if h["cosine"] < 0.45:
        continue
    nid = h["node_id"]
    if nid not in best_by_nid or h["cosine"] > best_by_nid[nid]["cosine"]:
        best_by_nid[nid] = h
sorted_hits = sorted(best_by_nid.values(), key=lambda x: -x["cosine"])[:15]
print(f"\n=== 聚合后 doc-level top 15 (按 node_id 取每 doc 最高 chunk) ===")
carwiki_doc_rank = None
for i, h in enumerate(sorted_hits, 1):
    meta = ds.get_meta(h["node_id"])
    title = (meta or {}).get("title", "?")[:40]
    mark = "  <== CARWIKI" if h["node_id"] == CARWIKI_NID else ""
    print(f"  {i:2d}. cos={h['cosine']:.3f} {title}{mark}")
    if h["node_id"] == CARWIKI_NID:
        carwiki_doc_rank = i

print(f"\n车载对接Wiki doc-level 排名: {carwiki_doc_rank}/15")

# === LLM judge_docs 决定最终 doc_picks ===
ctx_by_nid = {}
for h in sorted_hits:
    all_chunks = cs.get_doc_chunks(h["node_id"])
    pos = next((i for i, x in enumerate(all_chunks) if x["idx"] == h["chunk_idx"]), 0)
    ctx_by_nid[h["node_id"]] = get_chunk_with_context(all_chunks, hit_idx=pos, neighbors=1)

inputs = [
    {
        "node_id": h["node_id"],
        "title": (ds.get_meta(h["node_id"]) or {}).get("title") or "",
        "summary": ctx_by_nid.get(h["node_id"], "")[:1500],
    }
    for h in sorted_hits
]
verdicts = judge_docs(text, inputs)
print(f"\n=== LLM judge_docs ({len(verdicts)} verdicts) ===")
carwiki_judged = None
for v in verdicts:
    related = v.get("related")
    score = v.get("score")
    nid = v.get("node_id")
    title = next((i["title"] for i in inputs if i["node_id"] == str(nid)), "?")[:30]
    mark = "  <== CARWIKI" if str(nid) == CARWIKI_NID else ""
    print(f"  related={related} score={score} {title}{mark}")
    if str(nid) == CARWIKI_NID:
        carwiki_judged = v

print("\n========== 结论 ==========")
print(f"车载对接Wiki:")
print(f"  - chunk KNN 是否命中？  {'是' if carwiki_chunk_hits else '否'}")
print(f"  - doc-level 排名？      {carwiki_doc_rank}")
print(f"  - LLM judge?            {carwiki_judged}")
