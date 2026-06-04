"""Redmine MySQL 直连封装。

只读 + 流式：
  - iter_issues_for_backfill(): 按 id 翻页流式拉所有 issue
  - iter_issues_updated_since(ts): 增量拉 updated_on > ts 的 issue
  - fetch_journals_bulk(ids): 一次性拉一批 issue 的 journals + status 变更明细
  - get_status_map(): 拿 issue_statuses 表（id → name + is_closed）

注意：notes 字段可能含大富文本/HTML，原样返回，上层 text_cleaner 清洗。
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from typing import Iterator

import pymysql
from pymysql.cursors import SSDictCursor  # 服务端游标，流式

from .config import cfg


def _conn_kwargs() -> dict:
    c = cfg()["redmine_db"]
    return dict(
        host=c["host"],
        port=int(c["port"]),
        user=c["user"],
        password=c["password"],
        database=c["database"],
        charset=c.get("charset", "utf8mb4"),
        autocommit=True,
        # 不要 cursorclass 写死，按 caller 决定
    )


class RedmineDB:
    def __init__(self) -> None:
        self.kw = _conn_kwargs()
        self.page_size = int(cfg()["redmine_db"].get("backfill_page_size", 500))
        self._status_cache: dict[int, dict] | None = None

    # 用 contextmanager 管理连接，避免长连接被 server 切断
    @contextmanager
    def _conn(self, server_side: bool = False):
        kw = dict(self.kw)
        conn = pymysql.connect(**kw)
        try:
            cursor_cls = SSDictCursor if server_side else pymysql.cursors.DictCursor
            cur = conn.cursor(cursor_cls)
            try:
                yield conn, cur
            finally:
                cur.close()
        finally:
            conn.close()

    def get_descendant_project_ids(self, root_id: int) -> list[int]:
        """用 lft/rgt 嵌套集合一次性拿 root 项目的全部子孙（含 root 自身）。

        Redmine projects 表用 nested set 维护项目树，O(1) 查询全部子孙。
        只返回 status=1（active）的项目，已归档/已关闭的不参与触发。
        """
        with self._conn() as (_, cur):
            cur.execute(
                """SELECT id FROM projects
                    WHERE status = 1
                      AND lft >= (SELECT lft FROM projects WHERE id=%s)
                      AND rgt <= (SELECT rgt FROM projects WHERE id=%s)""",
                (root_id, root_id),
            )
            return [r["id"] for r in cur.fetchall()]

    def get_status_map(self) -> dict[int, dict]:
        """{status_id: {"name": ..., "is_closed": bool}}"""
        if self._status_cache is not None:
            return self._status_cache
        with self._conn() as (_, cur):
            cur.execute("SELECT id, name, is_closed FROM issue_statuses")
            self._status_cache = {
                r["id"]: {"name": r["name"], "is_closed": bool(r["is_closed"])}
                for r in cur.fetchall()
            }
        return self._status_cache

    def count_issues(self) -> int:
        with self._conn() as (_, cur):
            cur.execute("SELECT COUNT(*) AS n FROM issues")
            return int(cur.fetchone()["n"])

    def iter_issues_for_backfill(
        self, project_ids: list[int] | None = None
    ) -> Iterator[dict]:
        """按 id 翻页流式拉。返回的 dict 含基础字段，不含 journals。"""
        last_id = 0
        with self._conn() as (_, cur):
            while True:
                params: list = [last_id]
                proj_clause = ""
                if project_ids:
                    proj_clause = (
                        " AND project_id IN ("
                        + ",".join(str(int(p)) for p in project_ids)
                        + ")"
                    )
                cur.execute(
                    f"""SELECT id, project_id, tracker_id, status_id, subject,
                              description, closed_on, updated_on, created_on
                          FROM issues
                         WHERE id > %s {proj_clause}
                         ORDER BY id ASC
                         LIMIT {self.page_size}""",
                    params,
                )
                rows = cur.fetchall()
                if not rows:
                    return
                for r in rows:
                    yield r
                last_id = rows[-1]["id"]

    def iter_issues_updated_since(
        self,
        since: dt.datetime,
        project_ids: list[int] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """增量拉。返回 list（一般每次几十-几百条）。"""
        with self._conn() as (_, cur):
            params: list = [since]
            proj_clause = ""
            if project_ids:
                proj_clause = (
                    " AND project_id IN ("
                    + ",".join(str(int(p)) for p in project_ids)
                    + ")"
                )
            limit_clause = (
                f" LIMIT {int(limit)}"
                if limit
                else f" LIMIT {int(cfg()['redmine_db'].get('sync_batch_size', 5000))}"
            )
            cur.execute(
                f"""SELECT id, project_id, tracker_id, status_id, subject,
                          description, closed_on, updated_on, created_on
                      FROM issues
                     WHERE updated_on > %s {proj_clause}
                     ORDER BY updated_on ASC, id ASC
                     {limit_clause}""",
                params,
            )
            return list(cur.fetchall())

    def fetch_journals_bulk(self, issue_ids: list[int]) -> dict[int, list[dict]]:
        """一次性拉一批 issue 的全部 journals + journal_details。

        返回 {issue_id: [{id, notes, created_on, status_changed_to_id}]}
        status_changed_to_id 通过 join journal_details 拼出（property='attr' AND prop_key='status_id'）
        """
        if not issue_ids:
            return {}
        with self._conn() as (_, cur):
            placeholders = ",".join("%s" for _ in issue_ids)
            cur.execute(
                f"""SELECT j.id, j.journalized_id AS issue_id, j.notes, j.created_on,
                          MAX(CASE WHEN d.property='attr' AND d.prop_key='status_id'
                                   THEN d.value END) AS status_changed_to_id
                     FROM journals j
                LEFT JOIN journal_details d ON d.journal_id = j.id
                    WHERE j.journalized_type = 'Issue'
                      AND j.journalized_id IN ({placeholders})
                 GROUP BY j.id
                 ORDER BY j.journalized_id, j.id ASC""",
                tuple(issue_ids),
            )
            out: dict[int, list[dict]] = {}
            for r in cur.fetchall():
                iid = r["issue_id"]
                out.setdefault(iid, []).append(
                    {
                        "id": r["id"],
                        "notes": r["notes"] or "",
                        "created_on": r["created_on"],
                        "status_changed_to_id": (
                            int(r["status_changed_to_id"])
                            if r["status_changed_to_id"]
                            else None
                        ),
                    }
                )
            return out
