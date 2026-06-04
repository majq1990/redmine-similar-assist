"""Redmine REST 薄封装。所有调用走 X-Redmine-API-Key + JSON。"""
from __future__ import annotations

from typing import Any, Iterator

import requests
import urllib3
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import cfg

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class RedmineClient:
    def __init__(self) -> None:
        c = cfg()["redmine"]
        self.base = c["base_url"].rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-Redmine-API-Key"] = c["api_key"]
        self.session.headers["Content-Type"] = "application/json"
        self.session.verify = c.get("verify_ssl", True)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    def _put(self, path: str, data: dict) -> int:
        r = self.session.put(f"{self.base}{path}", json=data, timeout=30)
        # PUT /issues 成功返回 204 / 200
        r.raise_for_status()
        return r.status_code

    def get_issue(self, issue_id: int, include: str = "journals") -> dict:
        return self._get(f"/issues/{issue_id}.json", {"include": include})["issue"]

    def iter_project_issues(
        self, project_id: int, status_id: str = "*", page_size: int = 100
    ) -> Iterator[dict]:
        offset = 0
        while True:
            data = self._get(
                "/issues.json",
                {
                    "project_id": project_id,
                    "status_id": status_id,
                    "limit": page_size,
                    "offset": offset,
                    "sort": "id:asc",
                },
            )
            issues = data.get("issues", [])
            for it in issues:
                yield it
            total = data.get("total_count", 0)
            offset += len(issues)
            if not issues or offset >= total:
                break

    def add_note(self, issue_id: int, note: str) -> int:
        return self._put(f"/issues/{issue_id}.json", {"issue": {"notes": note}})
