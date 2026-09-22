"""FastAPI 接口测试（TestClient + 临时 SQLite）。"""
import os
import tempfile

import pytest

# 在导入应用前指定临时数据库
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


OLD_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled", "pending"]},
    },
}
NEW_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["active", "disabled"]},
        "owner": {"type": "string"},
    },
    "required": ["status", "owner"],
}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_and_get_analysis(client):
    resp = client.post("/api/analyses", json={
        "name": "user-schema-v1-v2",
        "old_schema": OLD_SCHEMA,
        "new_schema": NEW_SCHEMA,
        "samples": [{"status": "pending"}],
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    analysis_id = data["id"]
    assert data["report"]["backward_compatible"] is False
    assert data["report"]["forward_compatible"] is True
    assert data["report"]["summary"]["errors"] >= 1

    # 反例字段存在
    issues = data["report"]["issues"]
    backward = [i for i in issues if i["direction"] == "backward"]
    assert backward
    assert any(i["counterexample"] is not None for i in backward)

    # 样例结果
    assert data["report"]["sample_results"][0]["valid_against_new"] is False

    # 查询报告
    resp2 = client.get(f"/api/analyses/{analysis_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == analysis_id

    # 列表
    resp3 = client.get("/api/analyses")
    assert resp3.status_code == 200
    assert resp3.json()["total"] >= 1


def test_get_missing_returns_404(client):
    resp = client.get("/api/analyses/999999")
    assert resp.status_code == 404


def test_invalid_schema_returns_400(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "not-a-real-type"},
        "new_schema": {"type": "string"},
    })
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["field"] == "old_schema"


def test_compatible_schemas(client):
    resp = client.post("/api/analyses", json={
        "old_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
        "new_schema": {"type": "object",
                       "properties": {"a": {"type": "string"},
                                      "b": {"type": "integer"}}},
    })
    assert resp.status_code == 201
    report = resp.json()["report"]
    assert report["backward_compatible"]
    assert report["forward_compatible"]
    assert report["summary"]["errors"] == 0


def test_conditional_if_then_else_end_to_end(client):
    """draft-07+ 条件分支：then 收紧 -> 向后破坏，issue 定位到 #/then。"""
    if_a = {"required": ["kind"], "properties": {"kind": {"const": "a"}}}
    old = {"type": "object",
           "properties": {"kind": {"enum": ["a", "b"]}},
           "required": ["kind"],
           "if": if_a, "then": {"required": ["x"]}}
    new = {"type": "object",
           "properties": {"kind": {"enum": ["a", "b"]}},
           "required": ["kind"],
           "if": if_a, "then": {"required": ["x", "y"]}}
    resp = client.post("/api/analyses", json={
        "name": "conditional", "old_schema": old, "new_schema": new,
    })
    assert resp.status_code == 201, resp.text
    report = resp.json()["report"]
    assert report["backward_compatible"] is False
    assert report["forward_compatible"] is True
    issue = [i for i in report["issues"] if i["direction"] == "backward"][0]
    assert issue["code"] == "required_added"
    assert issue["path"] == "#/then"
    assert issue["severity"] == "error"
    assert issue["counterexample"]["kind"] == "a"


# ---------------------------------------------------------------------------
# POST /api/analyses/compare
# ---------------------------------------------------------------------------

COMPARE_OLD = {
    "type": "object",
    "properties": {"a": {"type": "string"}},
    "required": ["a"],
}
# v2：a 收紧 minLength、新增必填 b
COMPARE_V2 = {
    "type": "object",
    "properties": {"a": {"type": "string", "minLength": 2},
                   "b": {"type": "integer"}},
    "required": ["a", "b"],
}
# v3：b 不再必填（修复）、新增必填 c（新引入）、a 的 minLength 保持（仍存在）
COMPARE_V3 = {
    "type": "object",
    "properties": {"a": {"type": "string", "minLength": 2},
                   "c": {"type": "string"}},
    "required": ["a", "c"],
}


def _create(client, old_schema, new_schema, name=None):
    resp = client.post("/api/analyses", json={
        "name": name, "old_schema": old_schema, "new_schema": new_schema,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_compare_added_fixed_persisting(client):
    baseline = _create(client, COMPARE_OLD, COMPARE_V2, "baseline")
    current = _create(client, COMPARE_OLD, COMPARE_V3, "current")

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["baseline_id"] == baseline["id"]
    assert data["current_id"] == current["id"]
    assert data["counts"] == {"added": 1, "fixed": 1, "persisting": 1,
                              "severity_changed": 0}

    added = data["issues"]["added"]
    fixed = data["issues"]["fixed"]
    persisting = data["issues"]["persisting"]
    assert [i["code"] for i in added] == ["required_added"]
    assert "'c'" in added[0]["reason"]
    assert [i["code"] for i in fixed] == ["required_added"]
    assert "'b'" in fixed[0]["reason"]
    assert [i["code"] for i in persisting] == ["min_length_increased"]

    # 每条对比 issue 都带稳定指纹
    for group in (added, fixed, persisting):
        for entry in group:
            assert len(entry["fingerprint"]) == 16

    # 双向兼容性：两个方向都保持不兼容 -> changed 为 False
    assert data["compatibility"]["backward"] == {
        "baseline": False, "current": False, "changed": False,
    }
    assert data["compatibility"]["forward"] == {
        "baseline": True, "current": True, "changed": False,
    }

    # 错误 / 警告汇总差异
    diff = data["summary_diff"]
    assert diff["errors"]["baseline"] == baseline["report"]["summary"]["errors"]
    assert diff["errors"]["current"] == current["report"]["summary"]["errors"]
    assert diff["errors"]["delta"] == (
        current["report"]["summary"]["errors"]
        - baseline["report"]["summary"]["errors"]
    )
    assert diff["warnings"]["delta"] == 0
    assert diff["backward"]["errors"]["baseline"] == 2


def test_compare_severity_only_change(client):
    # 同一 integer_required 问题：baseline 可构造验证反例（error），
    # current 中 maximum 限制使反例不再成立（降级 warning）
    baseline = _create(client, {"minimum": 0}, {"type": "integer"})
    current = _create(client, {"minimum": 0, "maximum": 1}, {"type": "integer"})

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    changed = data["issues"]["severity_changed"]
    assert data["counts"]["severity_changed"] == 1
    assert changed[0]["code"] == "integer_required"
    assert changed[0]["baseline_severity"] == "error"
    assert changed[0]["current_severity"] == "warning"
    # 仍存在：type_restricted 两份报告都有
    assert any(i["code"] == "type_restricted" for i in data["issues"]["persisting"])


def test_compare_does_not_modify_reports(client):
    baseline = _create(client, COMPARE_OLD, COMPARE_V2)
    current = _create(client, COMPARE_OLD, COMPARE_V3)

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": baseline["id"], "current_id": current["id"],
    })
    assert resp.status_code == 200

    for original in (baseline, current):
        after = client.get(f"/api/analyses/{original['id']}").json()
        assert after["report"] == original["report"]
        assert after["old_schema"] == original["old_schema"]
        assert after["new_schema"] == original["new_schema"]


def test_compare_self_all_persisting(client):
    analysis = _create(client, COMPARE_OLD, COMPARE_V2)
    resp = client.post("/api/analyses/compare", json={
        "baseline_id": analysis["id"], "current_id": analysis["id"],
    })
    assert resp.status_code == 200
    data = resp.json()
    total = analysis["report"]["summary"]["total"]
    assert data["counts"]["persisting"] == total
    assert data["counts"]["added"] == 0
    assert data["counts"]["fixed"] == 0


def test_compare_missing_analysis_returns_404(client):
    analysis = _create(client, COMPARE_OLD, COMPARE_V2)

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": 999999, "current_id": analysis["id"],
    })
    assert resp.status_code == 404

    resp = client.post("/api/analyses/compare", json={
        "baseline_id": analysis["id"], "current_id": 999999,
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/gate
# ---------------------------------------------------------------------------

# backward：required_added error（#）+ 两条 pattern_changed warning
# forward：两条 pattern_changed warning
GATE_OLD = {
    "type": "object",
    "properties": {
        "a": {"type": "string", "pattern": "^x"},
        "b": {"type": "string", "pattern": "^y"},
    },
}
GATE_NEW = {
    "type": "object",
    "properties": {
        "a": {"type": "string", "pattern": "^xx"},
        "b": {"type": "string", "pattern": "^yy"},
    },
    "required": ["b"],
}


def _gate_report(client):
    return _create(client, GATE_OLD, GATE_NEW, "gate-case")


def test_gate_error_blocks_and_counts(client):
    analysis = _gate_report(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pass"] is False
    counts = data["counts"]["backward"]
    assert counts["errors"] == 1
    assert counts["warnings"] == 2
    assert counts["exempted"] == 0
    assert counts["total"] == 3
    assert counts["max_warnings"] == 0
    codes = [(i["code"], i["blocked_reason"]) for i in data["blocking"]]
    assert ("required_added", "error") in codes
    assert sum(1 for _, reason in codes if reason == "warning_limit_exceeded") == 2
    assert data["exempted"] == []


def test_gate_warning_cap_pass_and_overflow(client):
    analysis = _gate_report(client)
    # cap=1：required error 豁免后只剩 2 条 warning，超 1 条
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "max_warnings": {"backward": 1},
        "exemptions": [
            {"direction": "backward", "code": "required_added", "path": "#"},
        ],
    })
    data = resp.json()
    assert data["pass"] is False
    assert [b["path"] for b in data["blocking"]] == ["#/properties/b"]
    assert data["blocking"][0]["blocked_reason"] == "warning_limit_exceeded"
    assert data["counts"]["backward"]["warnings"] == 2
    assert data["counts"]["backward"]["errors"] == 0
    assert data["exempted"][0]["rule_index"] == 0

    # cap=2：全部 warning 在额度内 -> 通过
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "max_warnings": {"backward": 2},
        "exemptions": [
            {"direction": "backward", "code": "required_added", "path": "#"},
        ],
    })
    assert resp.json()["pass"] is True
    assert resp.json()["blocking"] == []


def test_gate_wildcard_exemption_and_rule_index(client):
    analysis = _gate_report(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "exemptions": [
            # 下标 0：通配规则命中两条 pattern_changed
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/*"},
            # 下标 1：精确规则命中 required_added
            {"direction": "backward", "code": "required_added", "path": "#"},
        ],
    })
    data = resp.json()
    assert data["pass"] is True
    rule_indices = sorted((e["path"], e["rule_index"]) for e in data["exempted"])
    assert rule_indices == [
        ("#", 1),
        ("#/properties/a", 0),
        ("#/properties/b", 0),
    ]
    # 豁免项不计数
    assert data["counts"]["backward"] == {
        "errors": 0, "warnings": 0, "exempted": 3, "total": 3,
        "max_warnings": 0,
    }


def test_gate_exact_rule_beats_later_wildcard(client):
    analysis = _gate_report(client)
    # 两条规则都命中 #/properties/a 的 pattern_changed：
    # 下标 0 精确 path（3 精确字段），下标 1 通配 path（2 精确字段）；
    # 即使通配规则声明更晚，也必须记到精确规则。
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "exemptions": [
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/a"},
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/*"},
        ],
    })
    data = resp.json()
    hit_a = [e for e in data["exempted"] if e["path"] == "#/properties/a"]
    hit_b = [e for e in data["exempted"] if e["path"] == "#/properties/b"]
    assert len(hit_a) == 1 and hit_a[0]["rule_index"] == 0
    assert len(hit_b) == 1 and hit_b[0]["rule_index"] == 1
    # required_added 未被豁免 -> 门禁仍失败
    assert data["pass"] is False


def test_gate_unchecked_direction_ignored(client):
    analysis = _gate_report(client)
    # 只检查 forward：backward 的 error 完全不影响结论；forward 2 条 warning，cap=2
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["forward"],
        "max_warnings": {"forward": 2, "backward": 0},
    })
    data = resp.json()
    assert data["pass"] is True
    assert set(data["counts"]) == {"forward"}
    assert data["counts"]["forward"]["warnings"] == 2


def test_gate_clean_direction_passes(client):
    # 新增可选字段：两个方向都没有 issue
    analysis = _create(
        client,
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "object", "properties": {"a": {"type": "string"},
                                           "b": {"type": "integer"}}},
    )
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward", "forward"],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["pass"] is True
    assert data["blocking"] == []
    assert data["exempted"] == []
    assert data["counts"]["backward"]["total"] == 0
    assert data["counts"]["forward"]["total"] == 0


def test_gate_missing_analysis_returns_404(client):
    resp = client.post("/api/analyses/999999/gate", json={
        "directions": ["backward"],
    })
    assert resp.status_code == 404
    assert resp.json()["detail"] == "分析记录不存在"


def test_gate_validation_errors(client):
    analysis = _gate_report(client)
    base = f"/api/analyses/{analysis['id']}/gate"

    # directions 不能为空
    resp = client.post(base, json={"directions": []})
    assert resp.status_code == 422

    # 非法方向
    resp = client.post(base, json={"directions": ["sideways"]})
    assert resp.status_code == 422

    # 非法 path（不以 #/ 开头）
    resp = client.post(base, json={
        "directions": ["backward"],
        "exemptions": [{"direction": "backward", "code": "x", "path": "properties/a"}],
    })
    assert resp.status_code == 422

    # 负的 warning 上限
    resp = client.post(base, json={
        "directions": ["backward"], "max_warnings": {"backward": -1},
    })
    assert resp.status_code == 422

    # code 不能为空
    resp = client.post(base, json={
        "directions": ["backward"],
        "exemptions": [{"direction": "backward", "code": "", "path": "#"}],
    })
    assert resp.status_code == 422


def test_gate_does_not_modify_report(client):
    analysis = _gate_report(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/gate", json={
        "directions": ["backward"],
        "exemptions": [
            {"direction": "backward", "code": "required_added", "path": "#"},
            {"direction": "backward", "code": "pattern_changed",
             "path": "#/properties/*"},
        ],
    })
    assert resp.status_code == 200

    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == analysis["report"]
    assert after["old_schema"] == analysis["old_schema"]
    assert after["new_schema"] == analysis["new_schema"]


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/profile 合规画像
# ---------------------------------------------------------------------------

PROFILE_SCHEMA_OLD = {"type": "object"}
PROFILE_SCHEMA_NEW = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "age": {"type": "integer", "minimum": 0},
        "kind": {
            "oneOf": [
                {"type": "object",
                 "properties": {"t": {"const": "a"}, "v": {"type": "string"}},
                 "required": ["t", "v"]},
                {"type": "object",
                 "properties": {"t": {"const": "b"}, "v": {"type": "integer"}},
                 "required": ["t", "v"]},
            ]
        },
    },
    "required": ["name", "age"],
    "additionalProperties": False,
}


def _profile_analysis(client):
    return _create(client, PROFILE_SCHEMA_OLD, PROFILE_SCHEMA_NEW, "profile-case")


def test_profile_validates_against_new_schema(client):
    analysis = _profile_analysis(client)
    documents = [
        {"name": "ok", "age": 3, "kind": {"t": "a", "v": "x"}},  # 合规
        {"age": 1, "name": 2},                                    # name 类型错
        {"name": "z", "age": 1, "kind": {"t": "a", "v": 7}},      # oneOf 失败
    ]
    resp = client.post(f"/api/analyses/{analysis['id']}/profile",
                       json={"documents": documents})
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["analysis_id"] == analysis["id"]
    assert data["version"] == 1
    assert data["total_documents"] == 3
    assert data["valid_documents"] == 1
    assert data["invalid_documents"] == 2
    assert data["compliance_rate"] == 1 / 3
    for group in data["failures"]:
        assert len(group["fingerprint"]) == 16
        assert group["pointer"].startswith("#")
        assert group["count"] >= 1
        assert group["example_indices"] == sorted(group["example_indices"])

    # oneOf 失败归并到最深公共路径 #/kind
    kind = [g for g in data["failures"] if g["pointer"] == "#/kind"]
    assert len(kind) == 1
    assert kind[0]["validator"] == "oneOf"
    assert kind[0]["count"] == 1
    assert kind[0]["example_indices"] == [2]

    # 关联持久化：GET 最新快照与 POST 返回一致
    resp2 = client.get(f"/api/analyses/{analysis['id']}/profile")
    assert resp2.status_code == 200
    assert resp2.json() == data


def test_profile_versions_increment_and_listing(client):
    analysis = _profile_analysis(client)
    base = f"/api/analyses/{analysis['id']}"

    first = client.post(base + "/profile", json={"documents": [{"name": "a", "age": 1}]}).json()
    second = client.post(base + "/profile", json={"documents": [{"name": "b", "age": 2}]}).json()
    assert first["version"] == 1
    assert second["version"] == 2
    assert first["id"] != second["id"]

    # GET 最新返回 version=2
    latest = client.get(base + "/profile").json()
    assert latest["version"] == 2

    # 按 ID 取指定快照
    by_id = client.get(f"{base}/profiles/{first['id']}").json()
    assert by_id["version"] == 1

    # 列表
    resp = client.get(base + "/profiles")
    assert resp.status_code == 200
    listing = resp.json()
    assert listing["analysis_id"] == analysis["id"]
    assert listing["total"] == 2
    assert [item["version"] for item in listing["items"]] == [2, 1]
    summary = listing["items"][0]
    assert summary["total_documents"] == 1
    assert summary["valid_documents"] == 1
    assert summary["compliance_rate"] == 1.0
    assert summary["failure_groups"] == 0


def test_profile_order_independent_grouping(client):
    analysis = _profile_analysis(client)
    base = f"/api/analyses/{analysis['id']}"
    documents = [
        {"name": "y", "age": 1, "kind": {"t": "a", "v": 1}},
        {"name": "z", "age": 2, "kind": {"t": "a", "v": 2}},
        {"age": 3, "name": 4},
    ]
    a = client.post(base + "/profile", json={"documents": documents}).json()
    b = client.post(base + "/profile", json={"documents": list(reversed(documents))}).json()

    def fingerprint(result):
        return [(g["pointer"], g["validator"], g["fingerprint"], g["count"],
                 g["message"]) for g in result["failures"]]

    assert fingerprint(a) == fingerprint(b)


def test_profile_too_many_documents_returns_422_and_writes_nothing(client):
    analysis = _profile_analysis(client)
    base = f"/api/analyses/{analysis['id']}"

    # 5001 个文档 -> 422，且不写画像记录
    resp = client.post(base + "/profile",
                       json={"documents": [{"name": "x", "age": 1}] * 5001})
    assert resp.status_code == 422

    listing = client.get(base + "/profiles").json()
    assert listing["total"] == 0
    assert client.get(base + "/profile").status_code == 404

    # 空文档列表也 422
    assert client.post(base + "/profile", json={"documents": []}).status_code == 422


def test_profile_non_object_document_returns_422_and_writes_nothing(client):
    analysis = _profile_analysis(client)
    base = f"/api/analyses/{analysis['id']}"

    for bad in ([1, 2, 3], ["str"], [None], [True], [[]]):
        resp = client.post(base + "/profile", json={"documents": bad})
        assert resp.status_code == 422, bad

    # 混合列表中只要有非对象就 422
    resp = client.post(base + "/profile",
                       json={"documents": [{"name": "x", "age": 1}, "not-an-object"]})
    assert resp.status_code == 422

    listing = client.get(base + "/profiles").json()
    assert listing["total"] == 0


def test_profile_does_not_modify_analysis(client):
    analysis = _profile_analysis(client)
    client.post(f"/api/analyses/{analysis['id']}/profile",
                json={"documents": [{"name": "x", "age": 1}]})
    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == analysis["report"]
    assert after["new_schema"] == analysis["new_schema"]
    assert after["old_schema"] == analysis["old_schema"]


def test_profile_missing_analysis_returns_404(client):
    assert client.post("/api/analyses/999999/profile",
                       json={"documents": [{}]}).status_code == 404
    assert client.get("/api/analyses/999999/profile").status_code == 404
    assert client.get("/api/analyses/999999/profiles").status_code == 404


def test_profile_cross_analysis_id_is_404(client):
    a1 = _profile_analysis(client)
    a2 = _profile_analysis(client)
    created = client.post(f"/api/analyses/{a1['id']}/profile",
                          json={"documents": [{"name": "x", "age": 1}]}).json()
    # 画像属于 a1，用 a2 的命名空间访问 -> 404
    resp = client.get(f"/api/analyses/{a2['id']}/profiles/{created['id']}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/analyses/{id}/replay 样本回放诊断
# ---------------------------------------------------------------------------

REPLAY_OLD = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["admin", "editor", "viewer"]},
        "age": {"type": "integer"},
    },
}
REPLAY_NEW = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ["admin", "editor"]},
        "age": {"type": "integer", "minimum": 0},
    },
    "required": ["role"],
    "additionalProperties": False,
}


def _replay_analysis(client):
    return _create(client, REPLAY_OLD, REPLAY_NEW, "replay-case")


def test_replay_classifies_and_attributes_hits(client):
    analysis = _replay_analysis(client)
    documents = [
        {"role": "viewer", "age": 1},        # 0 backward: enum 移除
        {"role": "admin", "age": -1},        # 1 backward: minimum 收紧
        {"age": 9},                          # 2 backward: required_added
        {"role": "admin", "age": 1, "x": 2}, # 3 两版均合法（旧版允许额外键）？
        "not-an-object",                     # 4 两版均拒绝
        {"role": "admin", "age": 1, "z": 1}, # 5 新拒绝(额外键)、旧合法 -> backward
    ]
    # 文档 3 携带额外键：旧版默认允许，但新版 additionalProperties=false 拒绝
    resp = client.post(f"/api/analyses/{analysis['id']}/replay",
                       json={"documents": documents})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_documents"] == 6
    assert data["backward_failures"] == 5
    assert data["forward_failures"] == 0
    assert data["valid_documents"] == 0
    assert data["invalid_under_both"] == 1

    hits = {(h["direction"], h["path"], h["code"]): h for h in data["issue_hits"]}
    enum_hit = hits[("backward", "#/properties/role", "enum_value_removed")]
    assert enum_hit["hit_count"] == 1
    assert enum_hit["example_indices"] == [0]
    min_hit = hits[("backward", "#/properties/age", "minimum_increased")]
    assert min_hit["example_indices"] == [1]
    required_hit = hits[("backward", "#", "required_added")]
    assert required_hit["example_indices"] == [2]
    ap_hit = hits[("backward", "#", "additional_properties_forbidden")]
    assert ap_hit["hit_count"] == 2
    assert ap_hit["example_indices"] == [3, 5]

    # 命中组全部带严重级别与报告内消息
    assert all(h["severity"] in ("error", "warning") for h in data["issue_hits"])
    assert all(h["hit_count"] >= 1 for h in data["issue_hits"])

    # 未归因：两版均拒绝的字符串文档
    unatt = data["unattributed_failures"]
    assert any(u["direction"] is None and u["example_indices"] == [4]
               for u in unatt)

    # 逐文档分类结果
    results = data["document_results"]
    assert [r["index"] for r in results] == list(range(6))
    assert results[0]["direction"] == "backward"
    assert results[0]["valid_against_old"] is True
    assert results[0]["valid_against_new"] is False
    assert results[4]["direction"] is None
    assert results[4]["valid_against_old"] is False
    assert results[4]["attributed_issue_count"] == 0


def test_replay_forward_direction_and_issue_mapping(client):
    # 新版新增可选属性 c，旧版 additionalProperties=false：
    # 带 c 的文档新合法、旧拒绝 -> forward，命中 property_no_longer_allowed
    old = {"type": "object",
           "properties": {"a": {"type": "string"}},
           "additionalProperties": False}
    new = {"type": "object",
           "properties": {"a": {"type": "string"}, "c": {"type": "integer"}}}
    analysis = _create(client, old, new)
    resp = client.post(f"/api/analyses/{analysis['id']}/replay",
                       json={"documents": [{"c": 3}]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["forward_failures"] == 1
    assert data["backward_failures"] == 0
    hit = data["issue_hits"][0]
    assert hit["direction"] == "forward"
    assert hit["example_indices"] == [0]

    result = data["document_results"][0]
    assert result["direction"] == "forward"
    assert result["valid_against_new"] is True
    assert result["valid_against_old"] is False


def test_replay_all_valid_documents(client):
    analysis = _replay_analysis(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/replay",
                       json={"documents": [
                           {"role": "admin", "age": 5},
                           {"role": "editor", "age": 0},
                       ]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["valid_documents"] == 2
    assert data["backward_failures"] == 0
    assert data["forward_failures"] == 0
    assert data["invalid_under_both"] == 0
    assert data["issue_hits"] == []
    assert data["unattributed_failures"] == []
    for result in data["document_results"]:
        assert result["direction"] is None
        assert result["attributed_issue_count"] == 0
        assert result["unattributed_error_count"] == 0


def test_replay_accepts_any_json_types(client):
    # 回放入参不限制为对象（与画像不同）：字符串 / 数字 / 数组 / null 均合法
    analysis = _replay_analysis(client)
    resp = client.post(f"/api/analyses/{analysis['id']}/replay",
                       json={"documents": ["x", 1, None, True, [1, 2]]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_documents"] == 5
    assert data["invalid_under_both"] == 5


def test_replay_does_not_modify_analysis_or_profiles(client):
    analysis = _replay_analysis(client)
    # 先写一版画像，回放后画像与分析都应保持不变
    profile_resp = client.post(
        f"/api/analyses/{analysis['id']}/profile",
        json={"documents": [{"role": "admin", "age": 1}]})
    assert profile_resp.status_code == 201
    profile_before = profile_resp.json()

    replay_resp = client.post(
        f"/api/analyses/{analysis['id']}/replay",
        json={"documents": [{"role": "viewer"}]})
    assert replay_resp.status_code == 200

    after = client.get(f"/api/analyses/{analysis['id']}").json()
    assert after["report"] == analysis["report"]
    assert after["old_schema"] == analysis["old_schema"]
    assert after["new_schema"] == analysis["new_schema"]
    assert after["samples"] == analysis["samples"]

    # 画像快照数量与内容不变
    listing = client.get(f"/api/analyses/{analysis['id']}/profiles").json()
    assert listing["total"] == 1
    latest = client.get(f"/api/analyses/{analysis['id']}/profile").json()
    assert latest == profile_before


def test_replay_missing_analysis_returns_404(client):
    resp = client.post("/api/analyses/999999/replay",
                       json={"documents": [{"role": "admin"}]})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "分析记录不存在"


def test_replay_validation_errors(client):
    analysis = _replay_analysis(client)
    base = f"/api/analyses/{analysis['id']}/replay"

    # 空文档列表 -> 422
    resp = client.post(base, json={"documents": []})
    assert resp.status_code == 422

    # 缺少 documents 字段 -> 422
    resp = client.post(base, json={})
    assert resp.status_code == 422

    # 超过 5000 个文档 -> 422
    resp = client.post(base, json={"documents": [{"role": "admin"}] * 5001})
    assert resp.status_code == 422


def test_replay_5000_documents_accepted(client):
    analysis = _replay_analysis(client)
    base = f"/api/analyses/{analysis['id']}/replay"
    resp = client.post(base, json={"documents": [
        {"role": "admin", "age": 1}] * 5000})
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_documents"] == 5000


def test_replay_unattributed_failure_grouping(client):
    # 两版 schema 完全一致：报告无 issue，违反共有约束的文档归入未归因
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    analysis = _create(client, schema, schema)
    resp = client.post(f"/api/analyses/{analysis['id']}/replay",
                       json={"documents": [{"a": "x"}, {"a": "y"}]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["issue_hits"] == []
    assert data["invalid_under_both"] == 2
    groups = data["unattributed_failures"]
    assert len(groups) == 1
    group = groups[0]
    assert group["direction"] is None
    assert group["pointer"] == "#/a"
    assert group["validator"] == "type"
    assert group["count"] == 2
    assert group["example_indices"] == [0, 1]
    assert len(group["fingerprint"]) == 16
