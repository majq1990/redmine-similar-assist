"""配置加载。优先读项目根 config.yaml，找不到则报错。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"


def load_config() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        sys.stderr.write(
            f"[config] config.yaml not found at {_CONFIG_PATH}. "
            "Copy config.example.yaml -> config.yaml and fill in keys.\n"
        )
        raise SystemExit(2)
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # 简单校验
    for required in ("redmine", "embedding", "llm", "storage", "recall", "webhook", "target_projects"):
        if required not in cfg:
            raise SystemExit(f"[config] missing section: {required}")
    return cfg


def project_root() -> Path:
    return _PROJECT_ROOT


# Lazy singleton
_cfg: dict[str, Any] | None = None


def cfg() -> dict[str, Any]:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


# === 项目白名单解析（含子孙树）===
_target_projects_cache: set[int] | None = None
_target_projects_cache_at: float = 0.0


def invalidate_target_project_cache() -> None:
    """强制下次 get_target_project_ids() 重新查 DB。

    sync 每次跑前调一次，保证新增/归档的项目立即反映。
    """
    global _target_projects_cache, _target_projects_cache_at
    _target_projects_cache = None
    _target_projects_cache_at = 0.0


def get_target_project_ids() -> set[int]:
    """返回当前生效的项目白名单（target_projects 列举 ∪ target_project_root_id 子孙）。

    带 TTL 缓存避免每次请求都查 DB。项目树新建/归档后自动刷新。
    返回空 set 表示"不过滤"（接受所有项目）。
    """
    global _target_projects_cache, _target_projects_cache_at
    c = cfg()
    ttl = int(c.get("target_project_cache_ttl_sec", 600))
    now = time.time()
    if _target_projects_cache is not None and (now - _target_projects_cache_at) < ttl:
        return _target_projects_cache

    listed = set(int(x) for x in (c.get("target_projects") or []))
    root_id = c.get("target_project_root_id")
    descendants: set[int] = set()
    if root_id:
        # 延迟 import 避免循环
        from .db_client import RedmineDB
        try:
            descendants = set(RedmineDB().get_descendant_project_ids(int(root_id)))
        except Exception as e:
            sys.stderr.write(f"[config] descendant resolve failed: {e}\n")
            # 回退到 listed（最坏不阻塞服务）
            descendants = set()

    merged = listed | descendants
    _target_projects_cache = merged
    _target_projects_cache_at = now
    sys.stderr.write(
        f"[config] target_project_ids resolved: listed={len(listed)} "
        f"root_descendants={len(descendants)} total={len(merged)}\n"
    )
    return merged


def is_project_targeted(project_id: int | None) -> bool:
    """Pipeline / webhook 调这个判断项目是否在范围内。

    空白名单（target_projects=[] 且 root_id 未配）= 不过滤，全放行。
    """
    if not project_id:
        return False
    ids = get_target_project_ids()
    if not ids:
        return True  # 完全空配置 = 不过滤
    return project_id in ids
