"""一次性批量 dry-run 多个 issue，生成对比报告 markdown。

Usage:
  python -m scripts.batch_dryrun 500194 499215 499201 498959 497309
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import cfg, project_root  # noqa
from src.pipeline import ingest_new_issue  # noqa

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("issue_ids", nargs="+", type=int)
    ap.add_argument("--out", type=str, default="data/batch_dryrun.md")
    args = ap.parse_args()

    # 先清 assist_log，确保重测
    log_path = project_root() / cfg()["storage"]["log_db"]
    conn = sqlite3.connect(str(log_path))
    for iid in args.issue_ids:
        conn.execute("DELETE FROM assist_log WHERE issue_id=?", (iid,))
    conn.commit()

    results = []
    for iid in args.issue_ids:
        try:
            res = ingest_new_issue(iid, dry_run=True)
        except Exception as e:
            res = {"error": str(e), "issue_id": iid}
        results.append(res)

    base = cfg()["redmine"]["base_url"].rstrip("/")
    lines = ["# 批量 dry-run 报告\n"]
    for r in results:
        iid = r.get("issue_id", "?")
        lines.append(f"\n---\n\n## Query #{iid}")
        lines.append(f"原工单: {base}/issues/{iid}\n")
        if r.get("skipped"):
            lines.append(f"⚠️ SKIPPED: `{r['skipped']}`")
            continue
        if r.get("error"):
            lines.append(f"❌ ERROR: `{r['error']}`")
            continue
        picks = r.get("picks") or []
        if not picks:
            lines.append("⚠️ 无召回（KNN 候选全部 < 0.65 阈值，或 LLM gate 判定全不相关）")
            continue
        lines.append(f"召回 {len(picks)} 条：\n")
        for i, p in enumerate(picks, 1):
            lines.append(f"### #{i} #{p['issue_id']} [置信度 {int(p['score']*100)}%]")
            lines.append(f"- 标题: {p.get('subject','')}")
            lines.append(f"- 链接: {base}/issues/{p['issue_id']}")
            if p.get("solution"):
                lines.append(f"- 解决方案: {p['solution']}")
        # 把准备回写的完整 note 也展示
        lines.append("\n<details><summary>📝 完整 note 文案预览</summary>\n")
        lines.append("```")
        lines.append(r.get("note") or "(空)")
        lines.append("```\n</details>\n")

    out_path = project_root() / args.out
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
