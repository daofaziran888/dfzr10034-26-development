"""元数据索引领域服务：字段级元数据解析、索引和搜索发现。

接收资产注册触发，维护元数据索引的搜索和发现能力。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class MetadataIndexService(BaseModuleService):
    MODULE = "metadata_index"
    ENTITY = "元数据条目"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["条目编号", "所属资产", "字段名称", "字段类型", "业务含义", "枚举值", "样例数据", "最后解析"]
    REQUIRED_FIELDS = ["条目编号", "所属资产", "字段名称"]
    STATUS_ORDER = ["待解析", "已索引", "解析异常", "已过期"]
    ACTION_RULES = {"触发解析": "已索引", "重建索引": "解析异常", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def discover_schema(self, asset_code: str, schema_map: list[dict[str, str]], version_tag: str = "") -> list[dict[str, Any]]:
        """从资产schema自动发现并索引元数据（Pipeline 1 核心方法）。

        version_tag 可选：登记条目所属的目录版本，供字段代际映射按版本归属；
        旧作业不传时行为与之前完全一致，过渡期照常运行。
        """
        entries = []
        for idx, field_def in enumerate(schema_map):
            meta = {
                "id": store.next_id(self.MODULE),
                "条目编号": f"{asset_code}-FLD-{idx+1:03d}",
                "所属资产": asset_code,
                "字段名称": field_def.get("name", ""),
                "字段类型": field_def.get("type", ""),
                "业务含义": field_def.get("meaning", ""),
                "枚举值": field_def.get("enum_values", ""),
                "样例数据": field_def.get("sample", ""),
                "最后解析": _time.strftime("%Y-%m-%d %H:%M:%S"),
                "版本标签": version_tag,
                "status": "已索引",
                "pending": False,
                "abnormal": False,
                "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            store.rows(self.MODULE).append(meta)
            entries.append(meta)

        self.emit_event("metadata_indexed", {
            "asset_code": asset_code,
            "field_count": len(entries),
            "version_tag": version_tag,
        })
        return entries

    def rebuild_index(self, entry_id: int) -> dict[str, Any] | None:
        """重建指定元数据条目的索引。"""
        meta = self.find(entry_id)
        if not meta:
            return None
        meta["最后解析"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        meta["status"] = "已索引"
        meta["abnormal"] = False
        store.log_change(
            self.MODULE, entry_id, "重建索引",
            before="解析异常", after="已索引",
            source="rebuild", scope=self.MODULE
        )
        return meta

    def find(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def search_metadata(self, keyword: str) -> list[dict[str, Any]]:
        """搜索元数据索引。"""
        results = []
        kw = keyword.casefold()
        for row in store.rows(self.MODULE):
            for field in self.LIST_FIELDS:
                if kw in str(row.get(field, "")).casefold():
                    results.append(row)
                    break
        return results

    def get_asset_fields(self, asset_code: str) -> list[dict[str, Any]]:
        """获取指定资产的字段元数据。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("所属资产") == asset_code
        ]