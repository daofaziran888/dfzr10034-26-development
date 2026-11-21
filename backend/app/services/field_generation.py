"""字段代际领域服务：字段稳定身份、目录代际映射、存量回填与跨版本反查。

索引流水线不再靠字段名称字符串判断归属：每个逻辑字段持有稳定的字段身份，
名称随目录版本换代——改名延续同一身份，名称复用（重建）另立身份。
版本履历消费跨代次映射，差异汇总区分改名与重建；旧目录版本可随时回读
已经确认的映射结果，存量元数据条目按资产与版本归属回填，无法唯一还原的
条目保留其原始索引依据。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 差异类别：改名延续身份，重建另立身份，两者在差异汇总中必须分开。
DIFF_RENAME = "改名"
DIFF_REBUILD = "重建"
DIFF_NEW = "新增"
DIFF_RETIRE = "退役"
DIFF_KINDS = (DIFF_RENAME, DIFF_REBUILD, DIFF_NEW, DIFF_RETIRE)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


class FieldGenerationService(BaseModuleService):
    MODULE = "field_generation"
    ENTITY = "代际映射"
    OPERATION_TERM = "登记"
    LIST_FIELDS = ["映射编号", "所属资产", "字段身份", "字段名称", "代际", "版本标签", "身份来源", "确认依据"]
    REQUIRED_FIELDS = ["映射编号", "所属资产", "字段身份", "字段名称"]
    STATUS_ORDER = ["生效中", "已退役", "待确认"]
    ACTION_RULES = {"确认映射": "生效中", "退役身份": "已退役"}
    NEGATIVE_ACTIONS = []

    # ── 内部工具 ──────────────────────────────────────────────

    def _rows(self, asset_code: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(self.MODULE))
        if asset_code is not None:
            rows = [r for r in rows if r.get("所属资产") == asset_code]
        return rows

    def _next_serial(self, prefix: str, field: str) -> str:
        best = 0
        for row in store.rows(self.MODULE):
            code = str(row.get(field, ""))
            if code.startswith(prefix):
                best = max(best, _to_int(code[len(prefix):]))
        return f"{prefix}{best + 1:04d}"

    def _active_rows(self, asset_code: str) -> list[dict[str, Any]]:
        return [r for r in self._rows(asset_code) if not str(r.get("退役版本", "") or "")]

    def _asset_version_map(self, asset_code: str) -> dict[str, int]:
        """版本标签 → 代际（int），含各窗口的生效版本与退役版本。"""
        versions: dict[str, int] = {}
        for row in self._rows(asset_code):
            versions.setdefault(str(row.get("版本标签", "")), _to_int(row.get("代际")))
            retired_at = str(row.get("退役版本", "") or "")
            if retired_at:
                versions.setdefault(retired_at, _to_int(row.get("退役代际")))
        versions.pop("", None)
        return versions

    @staticmethod
    def _covers(row: dict[str, Any], generation: int) -> bool:
        """窗口是否覆盖指定代际：生效代际 ≤ 目标代际 < 退役代际。"""
        opened = _to_int(row.get("代际"))
        retired = _to_int(row.get("退役代际"))
        return opened <= generation and (retired == 0 or generation < retired)

    @staticmethod
    def _window_brief(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "字段身份": row.get("字段身份", ""),
            "字段名称": row.get("字段名称", ""),
            "业务含义": row.get("业务含义", ""),
            "生效版本": row.get("版本标签", ""),
            "退役版本": row.get("退役版本", "") or "生效中",
            "代际": row.get("代际", ""),
            "身份来源": row.get("身份来源", ""),
        }

    def _retire_row(self, row: dict[str, Any], version_tag: str, generation: int, now: str) -> None:
        row["退役版本"] = version_tag
        row["退役代际"] = str(generation)
        row["status"] = "已退役"
        row["pending"] = False
        row["retired_at"] = now

    def _open_window(
        self, asset_code: str, field_id: str, name: str, col: dict[str, str],
        generation: int, version_tag: str, source: str, basis: str, now: str,
    ) -> dict[str, Any]:
        row = {
            "id": store.next_id(self.MODULE),
            "映射编号": self._next_serial("GEN-", "映射编号"),
            "所属资产": asset_code,
            "字段身份": field_id,
            "字段名称": name,
            "字段类型": col.get("type", ""),
            "业务含义": col.get("meaning", ""),
            "代际": str(generation),
            "版本标签": version_tag,
            "退役版本": "",
            "退役代际": "",
            "身份来源": source,
            "确认依据": basis,
            "status": "生效中",
            "pending": False,
            "abnormal": False,
            "created_at": now,
        }
        store.rows(self.MODULE).append(row)
        return row

    # ── 目录版本登记（代际映射的核心写入路径） ─────────────────

    def register_catalog(
        self,
        asset_code: str,
        version_tag: str,
        columns: list[dict[str, str]],
        renames: dict[str, str] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """登记资产的一个目录版本，生成跨代次字段映射。

        columns: [{"name", "type", "meaning"}]，新版本的完整列清单。
        renames: {旧名: 新名}，由迁移方确认的改名声明；身份随声明延续。
        未声明改名而消失的旧名按退役处理；曾被占用的名称再次出现按重建
        处理（另立字段身份），与改名在差异汇总中严格区分。
        """
        now = _time.strftime("%Y-%m-%d %H:%M:%S")
        version_tag = str(version_tag or "").strip()
        renames = {str(k).strip(): str(v).strip() for k, v in (renames or {}).items()}
        cols = [
            {
                "name": str(c.get("name", "")).strip(),
                "type": str(c.get("type", "")).strip(),
                "meaning": str(c.get("meaning", "")).strip(),
            }
            for c in columns
        ]
        names = [c["name"] for c in cols if c["name"]]
        col_by_name = {c["name"]: c for c in cols if c["name"]}

        # ── 校验：任何错误都整体拒绝，不写半个版本 ──
        errors: list[str] = []
        if not version_tag:
            errors.append("版本标签不能为空")
        versions = self._asset_version_map(asset_code)
        if version_tag and version_tag in versions:
            errors.append(f"资产 {asset_code} 的版本 {version_tag} 已登记，不能重复")
        if not names:
            errors.append("目录至少包含一个字段")
        if len(names) != len(set(names)):
            errors.append("目录内存在重复字段名称")
        active = {r["字段名称"]: r for r in self._active_rows(asset_code)}
        renamed_olds = set(renames)
        rename_targets = set(renames.values())
        if len(rename_targets) != len(renames):
            errors.append("多条改名声明指向同一个新名称")
        for old, new in renames.items():
            if old == new:
                errors.append(f"改名声明的源列与目标列相同：{old}")
            if old not in active:
                errors.append(f"改名声明的源列 {old} 不在当前生效目录中")
            if new not in names:
                errors.append(f"改名声明的目标列 {new} 不在新目录中")
            if new in active and new not in renamed_olds:
                errors.append(f"改名目标 {new} 与仍在生效的同名列冲突")
        if errors:
            return {"ok": False, "errors": errors}

        generation = (max(versions.values()) if versions else 0) + 1
        ever_used = {r["字段名称"] for r in self._rows(asset_code)}
        diff: dict[str, list[dict[str, str]]] = {kind: [] for kind in DIFF_KINDS}
        created: list[dict[str, Any]] = []
        retired: list[dict[str, Any]] = []

        # 1) 改名：退役旧窗口，同一字段身份开新窗口。
        for old, new in renames.items():
            old_row = active[old]
            self._retire_row(old_row, version_tag, generation, now)
            retired.append(old_row)
            row = self._open_window(
                asset_code, old_row["字段身份"], new, col_by_name[new],
                generation, version_tag, "改名延续", f"改名声明:{old}→{new}", now,
            )
            created.append(row)
            diff[DIFF_RENAME].append({"字段": new, "源值": old, "目标值": new, "字段身份": old_row["字段身份"]})

        # 2) 退役：生效中、新目录没有、且未声明改名的列。
        for name, row in active.items():
            if name in renamed_olds or name in names:
                continue
            self._retire_row(row, version_tag, generation, now)
            retired.append(row)
            diff[DIFF_RETIRE].append({"字段": name, "源值": row["字段身份"], "目标值": "-", "字段身份": row["字段身份"]})

        # 3) 新列：延续的列不动作；名称曾被占用→重建（另立身份）；全新→新增。
        continuing = {n for n in names if n in active and n not in renamed_olds}
        for col in cols:
            name = col["name"]
            if not name or name in continuing or name in rename_targets:
                continue
            field_id = self._next_serial("FLD-", "字段身份")
            if name in ever_used:
                source, kind = "新名占用", DIFF_REBUILD
                previous = [r for r in self._rows(asset_code) if r.get("字段名称") == name]
                old_identity = previous[-1]["字段身份"] if previous else "-"
            else:
                source = "初始登记" if generation == 1 else "新增字段"
                kind = DIFF_NEW
                old_identity = "-"
            row = self._open_window(
                asset_code, field_id, name, col,
                generation, version_tag, source, reason or "目录登记", now,
            )
            created.append(row)
            diff[kind].append({"字段": name, "源值": old_identity, "目标值": field_id, "字段身份": field_id})

        # 4) 版本履历消费跨代次映射，差异汇总同步区分改名与重建。
        summary = self.summarize_diff(diff)
        version_entry = self._record_version(asset_code, version_tag, generation, names, summary, reason, now)
        diff_entries = self._record_diffs(asset_code, version_tag, diff, now)

        self.emit_event("catalog_generation_registered", {
            "asset_code": asset_code,
            "version_tag": version_tag,
            "generation": generation,
            "summary": summary,
        })
        return {
            "ok": True,
            "所属资产": asset_code,
            "版本标签": version_tag,
            "代际": generation,
            "diff": diff,
            "差异汇总": summary,
            "created": created,
            "retired": retired,
            "version_history": version_entry,
            "diff_entries": diff_entries,
        }

    @staticmethod
    def summarize_diff(diff: dict[str, list[dict[str, str]]]) -> str:
        parts = []
        for kind in DIFF_KINDS:
            items = diff.get(kind) or []
            if items:
                parts.append(f"{kind}:" + "、".join(item["字段"] for item in items))
        return "；".join(parts) if parts else "无变更"

    def _record_version(
        self, asset_code: str, version_tag: str, generation: int,
        names: list[str], summary: str, reason: str, now: str,
    ) -> dict[str, Any]:
        """版本履历消费跨代次映射：每个目录版本落一条版本快照。"""
        for row in store.rows("version_history"):
            if row.get("目标资产") == asset_code and row.get("status") == "当前版本":
                row["status"] = "历史版本"
        snap_id = store.next_id("version_history")
        entry = {
            "id": snap_id,
            "快照编号": f"VERS-{snap_id:04d}",
            "目标资产": asset_code,
            "版本标签": version_tag,
            "Schema摘要": ",".join(names),
            "行数记录": "0",
            "创建原因": reason or "代际映射登记",
            "差异字段": summary,
            "快照时间": now,
            "status": "当前版本",
            "pending": False,
            "abnormal": False,
            "created_at": now,
            "代际": str(generation),
        }
        store.rows("version_history").append(entry)
        return entry

    def _record_diffs(
        self, asset_code: str, version_tag: str,
        diff: dict[str, list[dict[str, str]]], now: str,
    ) -> list[dict[str, Any]]:
        """差异汇总落 diff_report：差异等级区分改名/重建/新增/退役。"""
        entries = []
        for kind in DIFF_KINDS:
            for item in diff.get(kind) or []:
                report_id = store.next_id("diff_report")
                entry = {
                    "id": report_id,
                    "记录编号": f"DIFF-{report_id:04d}",
                    "比较对象": f"{asset_code}@{version_tag}",
                    "比较维度": "字段代际",
                    "差异字段": item["字段"],
                    "源值": item["源值"],
                    "目标值": item["目标值"],
                    "差异等级": kind,
                    "报告时间": now,
                    "status": "已生成",
                    "pending": False,
                    "abnormal": False,
                }
                store.rows("diff_report").append(entry)
                entries.append(entry)
        return entries

    # ── 目录读取：当前目录与历史版本回滚读 ─────────────────────

    def asset_versions(self, asset_code: str) -> list[dict[str, Any]]:
        """资产已登记的目录版本列表（代际升序），供新老读者做版本比对。"""
        versions = self._asset_version_map(asset_code)
        return [
            {"版本标签": tag, "代际": gen}
            for tag, gen in sorted(versions.items(), key=lambda kv: kv[1])
        ]

    def read_catalog(self, asset_code: str, version_tag: str | None = None) -> dict[str, Any]:
        """按版本读取目录：默认当前生效目录；指定版本则回滚读已确认映射。"""
        versions = self._asset_version_map(asset_code)
        if version_tag is None:
            if not versions:
                return {"ok": False, "errors": [f"资产 {asset_code} 尚未登记任何目录版本"]}
            generation = max(versions.values())
            version_tag = next(tag for tag, gen in versions.items() if gen == generation)
        else:
            version_tag = str(version_tag).strip()
            if version_tag not in versions:
                return {"ok": False, "errors": [f"资产 {asset_code} 未登记版本 {version_tag}"]}
            generation = versions[version_tag]
        windows = [r for r in self._rows(asset_code) if self._covers(r, generation)]
        windows.sort(key=lambda r: (_to_int(r.get("代际")), str(r.get("映射编号", ""))))
        return {
            "ok": True,
            "所属资产": asset_code,
            "版本标签": version_tag,
            "代际": generation,
            "columns": [self._window_brief(r) for r in windows],
        }

    # ── 反查：名称 → 历代身份；条目 → 归属说明 ─────────────────

    def explain_name(self, asset_code: str, name: str, version_tag: str | None = None) -> dict[str, Any]:
        """反查一个名称在资产内的历代占用；指定版本时给出该版本的唯一归属。"""
        name = str(name or "").strip()
        windows = [r for r in self._rows(asset_code) if r.get("字段名称") == name]
        windows.sort(key=lambda r: (_to_int(r.get("代际")), str(r.get("映射编号", ""))))
        result: dict[str, Any] = {
            "ok": True,
            "所属资产": asset_code,
            "字段名称": name,
            "历代占用": [self._window_brief(r) for r in windows],
        }
        if version_tag is not None:
            versions = self._asset_version_map(asset_code)
            version_tag = str(version_tag).strip()
            if version_tag not in versions:
                return {"ok": False, "errors": [f"资产 {asset_code} 未登记版本 {version_tag}"]}
            generation = versions[version_tag]
            hits = [r for r in windows if self._covers(r, generation)]
            result["版本标签"] = version_tag
            if len(hits) == 1:
                result["结果"] = "唯一命中"
                result["命中"] = self._window_brief(hits[0])
            elif not hits:
                result["结果"] = "未命中"
            else:  # 同一版本不可能有两个生效窗口，防御性分支
                result["结果"] = "歧义"
        else:
            identities = {r.get("字段身份") for r in windows}
            if not identities:
                result["结果"] = "无记录"
            elif len(identities) == 1:
                result["结果"] = "唯一身份"
            else:
                result["结果"] = "多代共用"
        return result

    def explain_index_entry(self, entry_id: int) -> dict[str, Any]:
        """反查一条元数据索引条目：说明它在什么资产版本上指向哪一列。"""
        entry = store.find("metadata_index", entry_id)
        if entry is None:
            return {"ok": False, "errors": [f"元数据条目 {entry_id} 不存在"]}
        asset = str(entry.get("所属资产", ""))
        name = str(entry.get("字段名称", ""))
        result: dict[str, Any] = {
            "ok": True,
            "条目编号": entry.get("条目编号", ""),
            "所属资产": asset,
            "字段名称": name,
            "归属状态": entry.get("归属状态", "未迁移"),
            "索引依据": entry.get("索引依据", f"原始名称:{name}"),
        }
        if entry.get("归属状态") == "已归属" and entry.get("字段身份"):
            field_id = entry["字段身份"]
            history = [self._window_brief(r) for r in self._rows(asset) if r.get("字段身份") == field_id]
            history.sort(key=lambda h: _to_int(h["代际"]))
            version = entry.get("版本标签", "") or "未标注"
            result["字段身份"] = field_id
            result["版本标签"] = entry.get("版本标签", "")
            result["身份履历"] = history
            trace = "；".join(
                f"{h['生效版本']}起列名{h['字段名称']}（{h['业务含义'] or '无含义'}，{h['退役版本']}退役）"
                if h["退役版本"] != "生效中"
                else f"{h['生效版本']}起列名{h['字段名称']}（{h['业务含义'] or '无含义'}，生效中）"
                for h in history
            )
            result["说明"] = (
                f"条目{entry.get('条目编号')}依据「{result['索引依据']}」归属字段身份{field_id}，"
                f"在资产{asset}版本{version}上指向列{name}；身份履历：{trace}"
            )
        else:
            holders = self.explain_name(asset, name).get("历代占用", [])
            result["候选身份"] = holders
            if holders:
                options = "；".join(
                    f"{h['字段身份']}（{h['生效版本']}~{h['退役版本']}，{h['业务含义'] or '无含义'}）"
                    for h in holders
                )
                result["说明"] = (
                    f"条目{entry.get('条目编号')}保留原始索引依据「{name}」，"
                    f"该名称在资产{asset}被{len({h['字段身份'] for h in holders})}代字段先后使用，"
                    f"无法唯一还原；候选：{options}"
                )
            else:
                result["说明"] = (
                    f"条目{entry.get('条目编号')}保留原始索引依据「{name}」，"
                    f"资产{asset}的代际映射中无此名称记录"
                )
        return result

    # ── 存量迁移：逐表回填字段身份 ─────────────────────────────

    def migrate_asset(self, asset_code: str) -> dict[str, Any]:
        """把存量元数据条目逐表迁移到新索引（按资产与版本归属回填）。

        - 条目带版本标签：在该版本窗口内解析名称，唯一命中即归属。
        - 条目无版本标签：名称在资产全部代际中只对应一个身份才可归属。
        - 无法唯一还原（名称多代共用或无记录）：保留原始索引依据，不伪造身份。
        迁移是增量注解，字段名称与原检索行为不变，旧作业在过渡期照常运行。
        """
        resolved: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for entry in store.rows("metadata_index"):
            if entry.get("所属资产") != asset_code:
                continue
            if entry.get("归属状态") == "已归属":
                continue
            name = str(entry.get("字段名称", ""))
            version = str(entry.get("版本标签", "") or "")
            hit: dict[str, Any] | None = None
            basis = ""
            candidates: list[dict[str, Any]] = []
            if version:
                res = self.explain_name(asset_code, name, version)
                if res.get("结果") == "唯一命中":
                    hit = res["命中"]
                    basis = f"版本窗口命中:{version}"
                else:
                    candidates = res.get("历代占用", [])
            else:
                holders = self.explain_name(asset_code, name).get("历代占用", [])
                identities = {h["字段身份"] for h in holders}
                if len(identities) == 1:
                    hit = holders[0]
                    basis = "名称唯一命中"
                else:
                    candidates = holders
            if hit:
                entry["字段身份"] = hit["字段身份"]
                entry["索引依据"] = basis
                entry["归属状态"] = "已归属"
                resolved.append({
                    "条目编号": entry.get("条目编号", ""),
                    "字段名称": name,
                    "字段身份": hit["字段身份"],
                    "索引依据": basis,
                })
            else:
                entry["归属状态"] = "保留原始依据"
                entry["索引依据"] = f"原始名称:{name}"
                entry["候选身份"] = "、".join(sorted({c["字段身份"] for c in candidates}))
                kept.append({
                    "条目编号": entry.get("条目编号", ""),
                    "字段名称": name,
                    "候选身份": entry["候选身份"],
                })
        store.log_change(
            self.MODULE, 0, "存量迁移",
            before="按名称索引", after=f"已归属{len(resolved)}条/保留原始依据{len(kept)}条",
            source="field_generation", scope=asset_code,
        )
        return {
            "ok": True,
            "所属资产": asset_code,
            "已归属": len(resolved),
            "保留原始依据": len(kept),
            "resolved": resolved,
            "kept": kept,
        }
