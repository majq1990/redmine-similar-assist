import sqlite3, hashlib, numpy as np
from src.embedder import Embedder
from src.text_cleaner import build_issue_text
from src.vector_store import get_doc_store, _unpack, _l2_normalize, _doc_id_to_int64

subject = "【朝阳运管服】20260611朝阳市运管服车辆轨迹对接支持"
desc = (
    "朝阳市目前更换政务网系统。由于政务网环境和互联网环境隔离，"
    "车辆对接的轨迹和视频都通过互联网传输。目前移动提供了互联网服务器和互联网映射ip。"
    "互联网服务器和政务网服务器可以通信。现场和设备厂商借了一台设备，"
    "设置推送互联网映射端口，但轨迹和视频无法观看。请支持。"
)
text = build_issue_text(subject, desc)
emb = Embedder().embed([text])[0]

nid = "AR4GpnMqJzML1Xr9saRkbzPBVKe0xjE3"
rowid = _doc_id_to_int64(nid)
conn = sqlite3.connect("/app/data/vectors.db")
conn.enable_load_extension(True)
import sqlite_vec
sqlite_vec.load(conn)

# 1. 直算 cosine(案件, 车载对接Wiki)
v_row = conn.execute("SELECT embedding FROM vec_docs WHERE rowid=?", (rowid,)).fetchone()
m_row = conn.execute("SELECT title, summary FROM docs_meta WHERE node_id=?", (nid,)).fetchone()
if v_row and m_row:
    doc_vec = _l2_normalize(_unpack(v_row[0], 1024))
    q = _l2_normalize(np.asarray(emb, dtype="float32"))
    cos = float(np.dot(q, doc_vec))
    print(f"[CARWIKI] cosine vs 504431 = {cos:.4f}")
    print(f"  title: {m_row[0]}")
    print(f"  summary len: {len(m_row[1] or '')}")
    print(f"  summary[:500]: {(m_row[1] or '')[:500]}")
else:
    print("doc 不在库里? vec_docs:", v_row, "docs_meta:", m_row)

# 2. KNN top 15，看排名
ds = get_doc_store()
top = ds.knn(emb, top=15)
print("\n=== KNN doc top 15 ===")
hit_at = None
for i, d in enumerate(top, 1):
    mark = "  <== CARWIKI" if d["node_id"] == nid else ""
    print(f"  {i:2}. cos={d['cosine']:.4f} {d['title'][:55]}{mark}")
    if d["node_id"] == nid:
        hit_at = i
print(f"\n车载对接Wiki KNN 排名: {hit_at}")
print("当前 config: docs_knn_top=8, docs_min_cosine=0.45")
