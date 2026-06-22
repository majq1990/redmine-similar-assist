"""B1-1 切片函数验证：跑真实「车载对接Wiki」走一遍 split_markdown_chunks。"""
import sys
sys.path.insert(0, "/app")
from src.dingtalk_mcp_client import DingtalkMcpClient
from src.text_cleaner import split_markdown_chunks, get_chunk_with_context

CARWIKI_NID = "AR4GpnMqJzML1Xr9saRkbzPBVKe0xjE3"

mcp = DingtalkMcpClient()
md = mcp.get_document_markdown(CARWIKI_NID)
print(f"raw md length: {len(md)}")
print(f"raw md preview[:300]:\n{md[:300]}\n---")

chunks = split_markdown_chunks(md)
print(f"\n=== chunked into {len(chunks)} pieces ===")
for c in chunks[:8]:
    print(
        f"\n[idx={c['idx']}] heading={c['heading']!r} "
        f"len={len(c['text'])}\n  {c['text'][:200]}..."
    )

# 验证 get_chunk_with_context
print("\n=== get_chunk_with_context(hit_idx=2, neighbors=1) ===")
ctx = get_chunk_with_context(chunks, hit_idx=min(2, len(chunks) - 1), neighbors=1)
print(f"context length: {len(ctx)}")
print(ctx[:500] + "...")

# 长度分布统计
print("\n=== chunk 长度分布 ===")
lengths = [len(c["text"]) for c in chunks]
print(f"total: {len(chunks)}, min={min(lengths)}, max={max(lengths)}, "
      f"avg={sum(lengths)//len(lengths)}")
short = sum(1 for l in lengths if l < 100)
mid = sum(1 for l in lengths if 100 <= l < 500)
long_ = sum(1 for l in lengths if l >= 500)
print(f"<100: {short} | 100-500: {mid} | >=500: {long_}")

# 模拟 4549 文档平均切片数（用这一篇粗估）
print(f"\n本篇 {len(chunks)} chunks，4549 文档预计 ~{len(chunks)*4549} chunks (上限粗估)")
