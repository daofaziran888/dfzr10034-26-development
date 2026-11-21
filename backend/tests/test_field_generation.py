"""字段索引代际映射测试：稳定字段身份、改名/重建甄别、存量回填与跨版本反查。

关键原则与既有管线测试一致：
- 每个测试使用唯一的资产编号避免相互干扰
- 使用 store.reset() 确保每个测试干净启动
- 通过业务字段查找新建的记录，不硬编码 entry_id
"""
import app.store as store_mod
from app.services.field_generation import FieldGenerationService
from app.services.metadata_index import MetadataIndexService


def _fresh():
    """重置存储并返回服务实例。"""
    store_mod.store.reset()
    return {
        "field_generation": FieldGenerationService(),
        "metadata_index": MetadataIndexService(),
    }


def _rows(module: str, asset: str) -> list:
    return [r for r in store_mod.store.rows(module) if r.get("所属资产") == asset or r.get("目标资产") == asset]


def _build_cust_asset(s, asset: str) -> None:
    """客户目录资产：v1 含 cust_id(客户键)，v2 改名 client_ref，v3 新字段复用 cust_id(渠道代码)。"""
    s["field_generation"].register_catalog(asset, "v1", [
        {"name": "cust_id", "type": "string", "meaning": "客户键"},
        {"name": "cust_name", "type": "string", "meaning": "客户名称"},
    ])
    s["field_generation"].register_catalog(asset, "v2", [
        {"name": "client_ref", "type": "string", "meaning": "客户键"},
        {"name": "cust_name", "type": "string", "meaning": "客户名称"},
    ], renames={"cust_id": "client_ref"}, reason="客户键改名")
    s["field_generation"].register_catalog(asset, "v3", [
        {"name": "client_ref", "type": "string", "meaning": "客户键"},
        {"name": "cust_name", "type": "string", "meaning": "客户名称"},
        {"name": "cust_id", "type": "string", "meaning": "渠道代码"},
    ], reason="接入渠道字段")


# ══════════════════════════════════════════════════════════════════
# 代际映射登记：稳定身份、改名延续、名称复用重建
# ══════════════════════════════════════════════════════════════════

class TestCatalogRegistration:

    def test_initial_catalog_assigns_stable_identities(self):
        s = _fresh()
        result = s["field_generation"].register_catalog("FG-A1", "v1", [
            {"name": "order_id", "type": "bigint", "meaning": "订单标识"},
            {"name": "amount", "type": "decimal", "meaning": "金额"},
        ])
        assert result["ok"] is True
        assert result["代际"] == 1
        rows = _rows("field_generation", "FG-A1")
        assert len(rows) == 2
        identities = {r["字段身份"] for r in rows}
        assert len(identities) == 2  # 每列一个稳定身份
        assert all(r["身份来源"] == "初始登记" for r in rows)
        assert all(r["status"] == "生效中" for r in rows)

    def test_declared_rename_preserves_identity(self):
        s = _fresh()
        s["field_generation"].register_catalog("FG-A2", "v1", [
            {"name": "cust_id", "type": "string", "meaning": "客户键"},
        ])
        before = _rows("field_generation", "FG-A2")[0]
        result = s["field_generation"].register_catalog("FG-A2", "v2", [
            {"name": "client_ref", "type": "string", "meaning": "客户键"},
        ], renames={"cust_id": "client_ref"})
        assert result["ok"] is True
        rows = _rows("field_generation", "FG-A2")
        assert len(rows) == 2
        old_window = [r for r in rows if r["字段名称"] == "cust_id"][0]
        new_window = [r for r in rows if r["字段名称"] == "client_ref"][0]
        # 同一字段身份延续，名称换代
        assert new_window["字段身份"] == before["字段身份"]
        assert new_window["身份来源"] == "改名延续"
        assert old_window["status"] == "已退役"
        assert old_window["退役版本"] == "v2"
        # 差异汇总归类为改名
        assert [i["字段"] for i in result["diff"]["改名"]] == ["client_ref"]
        assert result["diff"]["重建"] == []

    def test_name_reuse_creates_new_identity_not_rename(self):
        s = _fresh()
        _build_cust_asset(s, "FG-A3")
        rows = _rows("field_generation", "FG-A3")
        cust_windows = [r for r in rows if r["字段名称"] == "cust_id"]
        assert len(cust_windows) == 2
        identities = {r["字段身份"] for r in cust_windows}
        assert len(identities) == 2  # 同名两代，身份不同
        reused = [r for r in cust_windows if r["业务含义"] == "渠道代码"][0]
        assert reused["身份来源"] == "新名占用"
        assert reused["版本标签"] == "v3"
        assert reused["status"] == "生效中"

    def test_invalid_rename_declaration_rejected(self):
        s = _fresh()
        s["field_generation"].register_catalog("FG-A4", "v1", [
            {"name": "a_col", "type": "string", "meaning": "甲"},
        ])
        result = s["field_generation"].register_catalog("FG-A4", "v2", [
            {"name": "b_col", "type": "string", "meaning": "乙"},
        ], renames={"ghost_col": "b_col"})
        assert result["ok"] is False
        assert any("ghost_col" in e for e in result["errors"])
        # 校验失败不落任何映射
        assert len(_rows("field_generation", "FG-A4")) == 1

    def test_duplicate_version_rejected(self):
        s = _fresh()
        s["field_generation"].register_catalog("FG-A5", "v1", [
            {"name": "a_col", "type": "string", "meaning": "甲"},
        ])
        result = s["field_generation"].register_catalog("FG-A5", "v1", [
            {"name": "b_col", "type": "string", "meaning": "乙"},
        ])
        assert result["ok"] is False
        assert any("v1" in e for e in result["errors"])


# ══════════════════════════════════════════════════════════════════
# 版本履历消费与差异汇总：区分改名和重建
# ══════════════════════════════════════════════════════════════════

class TestVersionHistoryAndDiff:

    def test_version_history_consumes_generation_mapping(self):
        s = _fresh()
        _build_cust_asset(s, "FG-B1")
        versions = [r for r in store_mod.store.rows("version_history") if r.get("目标资产") == "FG-B1"]
        assert len(versions) == 3
        by_tag = {v["版本标签"]: v for v in versions}
        assert "改名" in by_tag["v2"]["差异字段"]
        assert "client_ref" in by_tag["v2"]["差异字段"]
        assert "重建" in by_tag["v3"]["差异字段"]
        assert "cust_id" in by_tag["v3"]["差异字段"]
        # 只有最新版本是当前版本
        currents = [v for v in versions if v["status"] == "当前版本"]
        assert len(currents) == 1 and currents[0]["版本标签"] == "v3"

    def test_diff_report_distinguishes_rename_and_rebuild(self):
        s = _fresh()
        _build_cust_asset(s, "FG-B2")
        diffs = [r for r in store_mod.store.rows("diff_report") if r.get("比较对象", "").startswith("FG-B2@")]
        levels = {(d["差异等级"], d["差异字段"]) for d in diffs}
        assert ("改名", "client_ref") in levels
        assert ("重建", "cust_id") in levels
        # 改名与重建是两条不同的差异记录
        rename_rows = [d for d in diffs if d["差异等级"] == "改名"]
        rebuild_rows = [d for d in diffs if d["差异等级"] == "重建"]
        assert len(rename_rows) == 1 and len(rebuild_rows) == 1
        assert rename_rows[0]["源值"] == "cust_id" and rename_rows[0]["目标值"] == "client_ref"
        # 重建记录新旧两个身份，说明名称被另立身份的字段占用
        assert rebuild_rows[0]["源值"] != rebuild_rows[0]["目标值"]


# ══════════════════════════════════════════════════════════════════
# 跨版本反查：迁移人员能说明每条索引在什么版本指向哪一列
# ══════════════════════════════════════════════════════════════════

class TestReverseLookup:

    def test_explain_name_across_generations(self):
        s = _fresh()
        _build_cust_asset(s, "FG-C1")
        result = s["field_generation"].explain_name("FG-C1", "cust_id")
        assert result["结果"] == "多代共用"
        holders = result["历代占用"]
        assert len(holders) == 2
        first, second = holders
        assert first["业务含义"] == "客户键" and first["生效版本"] == "v1" and first["退役版本"] == "v2"
        assert second["业务含义"] == "渠道代码" and second["生效版本"] == "v3" and second["退役版本"] == "生效中"

    def test_explain_name_at_specific_version(self):
        s = _fresh()
        _build_cust_asset(s, "FG-C2")
        v1 = s["field_generation"].explain_name("FG-C2", "cust_id", "v1")
        assert v1["结果"] == "唯一命中"
        assert v1["命中"]["业务含义"] == "客户键"
        v3 = s["field_generation"].explain_name("FG-C2", "cust_id", "v3")
        assert v3["结果"] == "唯一命中"
        assert v3["命中"]["业务含义"] == "渠道代码"
        # 同名两代身份不同
        assert v1["命中"]["字段身份"] != v3["命中"]["字段身份"]
        # v2 窗口内 cust_id 已改名为 client_ref，旧名查不到
        v2 = s["field_generation"].explain_name("FG-C2", "cust_id", "v2")
        assert v2["结果"] == "未命中"
        # client_ref 在 v2/v3 都指向客户键身份
        ref = s["field_generation"].explain_name("FG-C2", "client_ref", "v3")
        assert ref["命中"]["字段身份"] == v1["命中"]["字段身份"]

    def test_rollback_read_old_catalog_after_switch(self):
        s = _fresh()
        _build_cust_asset(s, "FG-C3")
        # 回滚旧目录：切换前的客户键视图仍然可读
        v1 = s["field_generation"].read_catalog("FG-C3", "v1")
        assert v1["ok"] is True
        cols = {c["字段名称"]: c for c in v1["columns"]}
        assert cols["cust_id"]["业务含义"] == "客户键"
        # 新目录：cust_id 是渠道代码，client_ref 才是客户键
        v3 = s["field_generation"].read_catalog("FG-C3", "v3")
        cols3 = {c["字段名称"]: c for c in v3["columns"]}
        assert cols3["cust_id"]["业务含义"] == "渠道代码"
        assert cols3["client_ref"]["业务含义"] == "客户键"
        assert cols3["client_ref"]["字段身份"] == cols["cust_id"]["字段身份"]
        # 未登记的版本读不到
        bad = s["field_generation"].read_catalog("FG-C3", "v9")
        assert bad["ok"] is False


# ══════════════════════════════════════════════════════════════════
# 存量回填：按资产与版本归属，无法唯一还原的保留原始索引依据
# ══════════════════════════════════════════════════════════════════

class TestBackfillMigration:

    def test_backfill_by_asset_and_version_attribution(self):
        s = _fresh()
        _build_cust_asset(s, "FG-D1")
        # 切换前的客户键索引（带版本归属）与切换后的渠道索引
        pre = s["metadata_index"].discover_schema("FG-D1", [
            {"name": "cust_id", "type": "string", "meaning": "客户键"},
        ], version_tag="v1")
        post = s["metadata_index"].discover_schema("FG-D1", [
            {"name": "cust_id", "type": "string", "meaning": "渠道代码"},
        ], version_tag="v3")
        result = s["field_generation"].migrate_asset("FG-D1")
        assert result["已归属"] == 2
        pre_entry = store_mod.store.find("metadata_index", pre[0]["id"])
        post_entry = store_mod.store.find("metadata_index", post[0]["id"])
        assert pre_entry["归属状态"] == "已归属"
        assert pre_entry["索引依据"] == "版本窗口命中:v1"
        assert post_entry["归属状态"] == "已归属"
        assert post_entry["索引依据"] == "版本窗口命中:v3"
        # 两条索引同名不同身份：迁移人员可说明各自指向
        assert pre_entry["字段身份"] != post_entry["字段身份"]
        pre_explain = s["field_generation"].explain_index_entry(pre[0]["id"])
        post_explain = s["field_generation"].explain_index_entry(post[0]["id"])
        assert "客户键" in pre_explain["说明"] and "v1" in pre_explain["说明"]
        assert "渠道代码" in post_explain["说明"] and "v3" in post_explain["说明"]

    def test_ambiguous_legacy_entry_keeps_original_basis(self):
        s = _fresh()
        _build_cust_asset(s, "FG-D2")
        # 存量条目：只有名称没有版本，名称被两代字段先后使用
        legacy = s["metadata_index"].discover_schema("FG-D2", [
            {"name": "cust_id", "type": "string", "meaning": "历史遗留"},
        ])
        # 另一个存量条目：名称从未被复用，可唯一还原
        unique = s["metadata_index"].discover_schema("FG-D2", [
            {"name": "cust_name", "type": "string", "meaning": "客户名称"},
        ])
        result = s["field_generation"].migrate_asset("FG-D2")
        assert result["已归属"] == 1
        assert result["保留原始依据"] == 1
        legacy_entry = store_mod.store.find("metadata_index", legacy[0]["id"])
        assert legacy_entry["归属状态"] == "保留原始依据"
        assert legacy_entry["索引依据"] == "原始名称:cust_id"
        assert "字段身份" not in legacy_entry  # 不伪造身份
        assert legacy_entry["字段名称"] == "cust_id"  # 原始依据原样保留
        assert "、" in legacy_entry["候选身份"]  # 两个候选身份都列出
        unique_entry = store_mod.store.find("metadata_index", unique[0]["id"])
        assert unique_entry["归属状态"] == "已归属"
        assert unique_entry["索引依据"] == "名称唯一命中"
        # 保留原始依据的条目也能反查说明
        explain = s["field_generation"].explain_index_entry(legacy[0]["id"])
        assert explain["归属状态"] == "保留原始依据"
        assert len(explain["候选身份"]) == 2
        assert "无法唯一还原" in explain["说明"]

    def test_migration_is_idempotent_and_scoped_per_asset(self):
        s = _fresh()
        _build_cust_asset(s, "FG-D3")
        s["metadata_index"].discover_schema("FG-D3", [
            {"name": "cust_name", "type": "string", "meaning": "客户名称"},
        ], version_tag="v2")
        first = s["field_generation"].migrate_asset("FG-D3")
        assert first["已归属"] == 1
        second = s["field_generation"].migrate_asset("FG-D3")
        assert second["已归属"] == 0  # 已归属的不重复迁移
        # 逐表迁移：其他资产的条目不受影响
        other = s["metadata_index"].discover_schema("FG-D3-OTHER", [
            {"name": "cust_name", "type": "string", "meaning": "别表字段"},
        ])
        s["field_generation"].migrate_asset("FG-D3")
        other_entry = store_mod.store.find("metadata_index", other[0]["id"])
        assert "归属状态" not in other_entry

    def test_old_jobs_still_run_during_transition(self):
        s = _fresh()
        _build_cust_asset(s, "FG-D4")
        s["metadata_index"].discover_schema("FG-D4", [
            {"name": "cust_id", "type": "string", "meaning": "渠道代码"},
        ], version_tag="v3")
        s["field_generation"].migrate_asset("FG-D4")
        # 旧作业按名称检索、按资产取字段，行为不变
        found = s["metadata_index"].search_metadata("cust_id")
        assert any(r.get("所属资产") == "FG-D4" for r in found)
        fields = s["metadata_index"].get_asset_fields("FG-D4")
        assert len(fields) == 1 and fields[0]["字段名称"] == "cust_id"


# ══════════════════════════════════════════════════════════════════
# 订单表状态列：改名后新列占用旧名，旧客户端与新目录分别重读
# ══════════════════════════════════════════════════════════════════

class TestOrdersStatusColumn:

    def _build_orders(self, s, asset: str) -> None:
        s["field_generation"].register_catalog(asset, "v1", [
            {"name": "order_id", "type": "bigint", "meaning": "订单标识"},
            {"name": "status", "type": "string", "meaning": "订单状态"},
        ])
        s["field_generation"].register_catalog(asset, "v2", [
            {"name": "order_id", "type": "bigint", "meaning": "订单标识"},
            {"name": "order_status", "type": "string", "meaning": "订单状态"},
        ], renames={"status": "order_status"}, reason="状态列改名")
        s["field_generation"].register_catalog(asset, "v3", [
            {"name": "order_id", "type": "bigint", "meaning": "订单标识"},
            {"name": "order_status", "type": "string", "meaning": "订单状态"},
            {"name": "status", "type": "string", "meaning": "处理状态"},
        ], reason="新列占用旧名")

    def test_rename_then_reuse_old_name(self):
        s = _fresh()
        self._build_orders(s, "FG-E1")
        rows = _rows("field_generation", "FG-E1")
        status_windows = [r for r in rows if r["字段名称"] == "status"]
        assert len(status_windows) == 2
        identities = {r["字段身份"] for r in status_windows}
        assert len(identities) == 2
        # 订单状态身份延续到 order_status
        order_status = [r for r in rows if r["字段名称"] == "order_status"][0]
        old_status = [r for r in status_windows if r["业务含义"] == "订单状态"][0]
        assert order_status["字段身份"] == old_status["字段身份"]
        assert order_status["身份来源"] == "改名延续"
        # 占用旧名的新列是重建
        new_status = [r for r in status_windows if r["业务含义"] == "处理状态"][0]
        assert new_status["身份来源"] == "新名占用"

    def test_old_client_and_new_catalog_reread_both_versions(self):
        s = _fresh()
        self._build_orders(s, "FG-E2")
        # 旧客户端按旧目录重读 v1：status 是订单状态
        v1 = s["field_generation"].read_catalog("FG-E2", "v1")
        cols1 = {c["字段名称"]: c for c in v1["columns"]}
        assert cols1["status"]["业务含义"] == "订单状态"
        # 旧客户端重读 v2：旧名已退役，改名延续到 order_status
        v2 = s["field_generation"].read_catalog("FG-E2", "v2")
        cols2 = {c["字段名称"]: c for c in v2["columns"]}
        assert "status" not in cols2
        assert cols2["order_status"]["字段身份"] == cols1["status"]["字段身份"]
        # 新目录重读 v3：status 指向处理状态新身份，订单状态在 order_status
        v3 = s["field_generation"].read_catalog("FG-E2", "v3")
        cols3 = {c["字段名称"]: c for c in v3["columns"]}
        assert cols3["status"]["业务含义"] == "处理状态"
        assert cols3["status"]["字段身份"] != cols1["status"]["字段身份"]
        assert cols3["order_status"]["字段身份"] == cols1["status"]["字段身份"]
        # 新老读者在迁移窗口内同时做版本比对：同名按版本各归各的身份
        old_reader = s["field_generation"].explain_name("FG-E2", "status", "v1")
        new_reader = s["field_generation"].explain_name("FG-E2", "status", "v3")
        assert old_reader["命中"]["字段身份"] != new_reader["命中"]["字段身份"]
        assert old_reader["命中"]["业务含义"] == "订单状态"
        assert new_reader["命中"]["业务含义"] == "处理状态"

    def test_version_comparison_summary_for_orders(self):
        s = _fresh()
        self._build_orders(s, "FG-E3")
        versions = [r for r in store_mod.store.rows("version_history") if r.get("目标资产") == "FG-E3"]
        by_tag = {v["版本标签"]: v for v in versions}
        assert "改名" in by_tag["v2"]["差异字段"]
        assert "重建" in by_tag["v3"]["差异字段"]
        # 回滚旧目录也能读回已确认结果
        rollback = s["field_generation"].read_catalog("FG-E3", "v1")
        assert rollback["ok"] is True
        assert any(c["字段名称"] == "status" for c in rollback["columns"])
