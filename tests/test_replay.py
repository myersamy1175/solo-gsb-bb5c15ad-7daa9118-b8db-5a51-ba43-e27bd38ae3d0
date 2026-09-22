"""样本回放诊断引擎单元测试（方向分类 + issue 归因 + 未归因归并）。"""
from app.compat import analyze
from app.compat import make_validator, resolve_refs
from app.replay import (
    MAX_REPLAY_DOCUMENTS,
    attribute_error,
    replay_samples,
)


def _first_error(schema, document):
    return next(iter(make_validator(schema).iter_errors(document)))


def _replay(old, new, documents, samples=None):
    report = analyze(old, new, samples)
    return replay_samples(old, new, report, documents), report


# ---------------------------------------------------------------------------
# 方向分类
# ---------------------------------------------------------------------------


def test_documents_classified_into_four_buckets():
    old = {"type": "object", "properties": {"a": {"type": "string"}},
           "required": ["a"]}
    new = {"type": "object", "properties": {"a": {"type": "string"},
                                            "b": {"type": "integer"}},
           "required": ["a", "b"]}
    docs = [
        {"a": "x"},                  # 旧合法、新拒绝 -> backward
        {"a": "x", "b": 1, "c": 2}, # 两版均合法（额外字段）-> valid
        42,                          # 两版均拒绝 -> invalid_both
    ]
    result, _ = _replay(old, new, docs)
    assert result["total_documents"] == 3
    assert result["backward_failures"] == 1
    assert result["forward_failures"] == 0
    assert result["valid_documents"] == 1
    assert result["invalid_under_both"] == 1
    assert result["document_results"][0]["direction"] == "backward"
    assert result["document_results"][1]["direction"] is None
    assert result["document_results"][1]["valid_against_old"] is True
    assert result["document_results"][2]["direction"] is None
    assert result["document_results"][2]["valid_against_old"] is False


def test_forward_failure_new_valid_old_rejected():
    # 新版新增可选属性 c 且旧版 additionalProperties=false：
    # 携带 c 的文档新合法、旧拒绝 -> forward
    old = {"type": "object", "properties": {"a": {"type": "string"}},
           "additionalProperties": False}
    new = {"type": "object",
           "properties": {"a": {"type": "string"}, "c": {"type": "integer"}}}
    result, _ = _replay(old, new, [{"c": 1}])
    assert result["forward_failures"] == 1
    assert result["backward_failures"] == 0
    assert result["document_results"][0]["direction"] == "forward"


# ---------------------------------------------------------------------------
# issue 归因
# ---------------------------------------------------------------------------


def test_required_added_attribution():
    old = {"type": "object", "properties": {"a": {"type": "string"}}}
    new = {"type": "object", "properties": {"a": {"type": "string"}},
           "required": ["a"]}
    result, report = _replay(old, new, [{}, {}])
    hit = result["issue_hits"][0]
    assert hit["code"] == "required_added"
    assert hit["path"] == "#"
    assert hit["direction"] == "backward"
    assert hit["hit_count"] == 2
    assert hit["example_indices"] == [0, 1]


def test_enum_and_nested_property_attribution():
    old = {"type": "object",
           "properties": {"role": {"type": "string",
                                    "enum": ["a", "b", "c"]}}}
    new = {"type": "object",
           "properties": {"role": {"type": "string", "enum": ["a", "b"]}}}
    result, _ = _replay(old, new, [{"role": "c"}, {"role": "c"}, {"role": "a"}])
    assert result["backward_failures"] == 2
    hit = result["issue_hits"][0]
    assert hit["code"] == "enum_value_removed"
    assert hit["path"] == "#/properties/role"
    assert hit["hit_count"] == 2
    assert hit["example_indices"] == [0, 1]


def test_numeric_and_string_bound_attribution():
    old = {"type": "object", "properties": {
        "n": {"type": "integer"}, "s": {"type": "string"}}}
    new = {"type": "object", "properties": {
        "n": {"type": "integer", "minimum": 10},
        "s": {"type": "string", "maxLength": 2}}}
    result, _ = _replay(old, new, [{"n": 3}, {"s": "abc"}, {"n": 11, "s": "ok"}])
    by_code = {h["code"]: h for h in result["issue_hits"]}
    assert by_code["minimum_increased"]["path"] == "#/properties/n"
    assert by_code["minimum_increased"]["example_indices"] == [0]
    assert by_code["max_length_decreased"]["path"] == "#/properties/s"
    assert by_code["max_length_decreased"]["example_indices"] == [1]
    assert result["valid_documents"] == 1


def test_type_removed_array_items_attribution():
    old = {"type": "object",
           "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    new = {"type": "object",
           "properties": {"tags": {"type": "array", "items": {"type": "integer"}}}}
    result, _ = _replay(old, new, [{"tags": ["x"]}])
    # 旧合法新拒绝 -> backward 命中 #/properties/tags/items type_removed
    back = [h for h in result["issue_hits"] if h["direction"] == "backward"]
    assert len(back) == 1
    assert back[0]["path"] == "#/properties/tags/items"
    assert back[0]["code"] == "type_removed"
    assert back[0]["example_indices"] == [0]


def test_draft7_tuple_element_attribution():
    old = {"items": [{"type": "integer"}]}
    new = {"items": [{"type": "string"}]}
    result, _ = _replay(old, new, [[1]])
    back = [h for h in result["issue_hits"] if h["direction"] == "backward"]
    assert len(back) == 1
    assert back[0]["path"] == "#/items/0"
    assert back[0]["code"] == "type_removed"


def test_schema_forbids_all_property_false_attribution():
    old = {"type": "object",
           "properties": {"x": {"type": "integer"}}}
    new = {"type": "object",
           "properties": {"x": False}}
    result, _ = _replay(old, new, [{"x": 1}])
    hit = result["issue_hits"][0]
    assert hit["code"] == "schema_forbids_all"
    assert hit["path"] == "#/properties/x"
    assert hit["example_indices"] == [0]


def test_schema_forbids_all_items_false_attribution():
    old = {"type": "array", "items": {"type": "integer"}}
    new = {"type": "array", "items": False}
    result, _ = _replay(old, new, [[1]])
    hit = result["issue_hits"][0]
    assert hit["code"] == "schema_forbids_all"
    assert hit["path"] == "#/items"
    assert hit["example_indices"] == [0]


def test_additional_properties_forbidden_attribution():
    old = {"type": "object"}
    new = {"type": "object", "additionalProperties": False}
    result, _ = _replay(old, new, [{"z": 1}])
    hit = result["issue_hits"][0]
    assert hit["code"] == "additional_properties_forbidden"
    assert hit["path"] == "#"
    assert hit["example_indices"] == [0]


def test_each_document_contributes_at_most_once_per_issue():
    old = {"type": "object", "properties": {"a": {"type": "string"}}}
    new = {"type": "object", "properties": {"a": {"type": "string"}},
           "required": ["a"]}
    # 单个文档只有一条 required 错误；即使内部实现遍历错误也只计一次
    result, _ = _replay(old, new, [{}])
    assert result["issue_hits"][0]["hit_count"] == 1
    assert result["issue_hits"][0]["example_indices"] == [0]


def test_sample_issue_attribution_by_instance_pointer():
    # 报告含样例 issue：sample_rejected_by_new，path 为实例错误位置
    old = {"type": "object"}
    new = {"type": "object", "required": ["a"]}
    samples = [{"b": 1}]
    report = analyze(old, new, samples)
    assert any(i["code"] == "sample_rejected_by_new" for i in report["issues"])
    # 回放同类文档：结构性 required_added 可命中时优先结构性归因；
    # 这里结构性 issue 与样例 issue 同时存在，命中取结构性。
    result = replay_samples(old, new, report, [{"c": 2}])
    codes = {h["code"] for h in result["issue_hits"]}
    assert "required_added" in codes


def test_hits_order_follows_report_order():
    old = {"type": "object", "properties": {
        "role": {"type": "string", "enum": ["a", "b"]}}}
    new = {"type": "object", "properties": {
        "role": {"type": "string", "enum": ["a"]}},
        "required": ["role"]}
    report = analyze(old, new)
    docs = [{"role": "b"}, {}]  # 先命中 enum，再命中 required
    result = replay_samples(old, new, report, docs)
    # 输出顺序按报告内 (direction, path, code) 排序，而非命中先后
    paths = [(h["path"], h["code"]) for h in result["issue_hits"]]
    assert paths == sorted(paths)
    by_code = {h["code"]: h for h in result["issue_hits"]}
    assert by_code["enum_value_removed"]["example_indices"] == [0]
    assert by_code["required_added"]["example_indices"] == [1]


# ---------------------------------------------------------------------------
# 无法归因
# ---------------------------------------------------------------------------


def test_unrelated_validation_failure_is_unattributed():
    # 旧/新两版都要求 a 为 integer（报告无兼容 issue）；
    # 文档因同一条规则被新版拒绝且旧版也拒绝 -> invalid_both，记入未归因
    old = {"type": "object", "properties": {"a": {"type": "integer"}}}
    new = {"type": "object", "properties": {"a": {"type": "integer"}}}
    result, report = _replay(old, new, [{"a": "x"}])
    assert report["summary"]["total"] == 0
    assert result["issue_hits"] == []
    assert result["invalid_under_both"] == 1
    assert len(result["unattributed_failures"]) == 1
    group = result["unattributed_failures"][0]
    assert group["direction"] is None
    assert group["pointer"] == "#/a"
    assert group["validator"] == "type"
    assert group["count"] == 1
    assert group["example_indices"] == [0]


def test_unattributed_merged_across_documents():
    old = {"type": "object", "properties": {"a": {"type": "integer"}}}
    new = {"type": "object", "properties": {"a": {"type": "integer"}}}
    result, _ = _replay(old, new, [{"a": "x"}, {"a": "y"}, {"a": 1}])
    assert result["valid_documents"] == 1
    group = result["unattributed_failures"][0]
    assert group["count"] == 2
    assert group["example_indices"] == [0, 1]


def test_unattributed_deduped_within_one_document_both_invalid():
    # 两版均拒绝时同一规则在旧、新各产生一条错误，文档内只计一次
    old = {"type": "object", "properties": {"a": {"type": "integer"}}}
    new = {"type": "object", "properties": {"a": {"type": "integer"}}}
    result, _ = _replay(old, new, [{"a": "x"}])
    assert result["unattributed_failures"][0]["count"] == 1
    assert result["document_results"][0]["unattributed_error_count"] == 1


def test_directional_failure_not_in_report_is_unattributed():
    # 报告没有 oneOf 相关 issue（两版一致），构造一个仅新版拒绝的
    # oneOf 失败场景：旧版无 oneOf，这种结构性差异报告会报 oneOf_added，
    # 因此改用两版都含 oneOf、但文档同时违反两版共有其他约束的情况——
    # 这里验证：方向错误存在但 (path, code) 在报告中缺失时归入未归因。
    # 做法：直接构造报告（剥离相关 issue）验证 attribute_error 行为。
    from app.replay import _build_issue_indexes

    old = {"type": "object", "properties": {
        "a": {"type": "integer", "minimum": 0}}}
    new = {"type": "object", "properties": {
        "a": {"type": "integer", "minimum": 10}}}
    report = analyze(old, new)
    # 伪造一份没有 minimum issue 的报告
    stripped = dict(report)
    stripped["issues"] = [
        i for i in report["issues"] if i["code"] != "minimum_increased"
    ]
    structural, sample = _build_issue_indexes(stripped)
    error = _first_error(resolve_refs(new), {"a": 5})
    assert attribute_error(error, "backward", structural, sample) is None
    result = replay_samples(old, new, stripped, [{"a": 5}])
    assert result["issue_hits"] == []
    assert result["backward_failures"] == 1
    un = result["unattributed_failures"]
    assert len(un) == 1
    assert un[0]["direction"] == "backward"
    assert un[0]["validator"] == "minimum"
    assert un[0]["pointer"] == "#/a"


# ---------------------------------------------------------------------------
# 纯函数 / 输入边界
# ---------------------------------------------------------------------------


def test_replay_does_not_mutate_inputs():
    old = {"type": "object", "properties": {"a": {"type": "string"}}}
    new = {"type": "object", "required": ["a"]}
    report = analyze(old, new)
    import copy
    report_snapshot = copy.deepcopy(report)
    replay_samples(old, new, report, [{}])
    assert report == report_snapshot


def test_empty_documents_allowed_at_engine_level():
    # 引擎本身不做数量校验（由 Pydantic 层负责），空列表应正常返回零计数
    result, _ = _replay({"type": "string"}, {"type": "string"}, [])
    assert result["total_documents"] == 0
    assert result["issue_hits"] == []
    assert result["unattributed_failures"] == []


def test_max_documents_constant_matches_profile():
    # 回放上限与画像一致，均为 5000
    assert MAX_REPLAY_DOCUMENTS == 5000


def test_non_object_documents_accepted_by_engine():
    # 回放接受任意 JSON 文档（与画像只收对象不同）
    old = {"type": "string"}
    new = {"type": "integer"}
    result, _ = _replay(old, new, ["x", 1, True, None, [1], {"a": 1}])
    assert result["backward_failures"] == 1   # "x"
    assert result["forward_failures"] == 1    # 1
    assert result["invalid_under_both"] == 4  # True/None/[1]/{...}


def test_ref_schemas_replay():
    old = {"$defs": {"p": {"type": "object", "required": ["k"]}},
           "$ref": "#/$defs/p"}
    new = {"$defs": {"p": {"type": "object", "required": ["k", "v"]}},
           "$ref": "#/$defs/p"}
    result, _ = _replay(old, new, [{"k": 1}])
    hit = result["issue_hits"][0]
    assert hit["code"] == "required_added"
    assert result["backward_failures"] == 1
