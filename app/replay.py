"""样本回放诊断引擎。

对一批真实文档分别用某次分析保存的**旧版 / 新版 JSON Schema** 做
``jsonschema`` 校验，按校验结果划分兼容方向，并把校验失败**归因到该分析
报告中已有的 issue**（以 ``direction + path + code`` 定位）：

* 旧版合法、新版拒绝 —— 向后回放失败（``backward``）；
* 新版合法、旧版拒绝 —— 向前回放失败（``forward``）；
* 两版均合法 / 均拒绝 —— 不构成兼容性失败。

归因规则
========
对每条方向明确的顶层校验错误：

1. 取触发规则在 schema 中的位置（``absolute_schema_path``），推导其
   "受约束节点"指针（如 ``.../minimum`` 退到属性节点
   ``#/properties/a``、``.../required`` 退到所属对象节点），同时生成
   跨 draft 别名（``prefixItems`` ↔ 列表形式 ``items``）；
2. 按 jsonschema 规则名（validator）推导可能对应的 issue 代码
   （如 ``required`` → ``required_added``、``minimum`` →
   ``minimum_increased``……）；
3. 在该方向报告 issue 的 (path, code) 索引中精确命中 —— 命中即归因；
4. 否则尝试归因到同路径的样例 issue（``sample_rejected_by_new/old``，
   引擎对真实样例以实例位置生成）；
5. 仍无法归因时记入 ``unattributed_failures``，按与画像一致的稳定
   指纹（位置 + 规则 + 规则主体）归并。

每个文档对同一条 issue 至多贡献一次命中；``hit_count`` 为命中文档数，
``example_indices`` 为示例文档下标（升序、有界）。

引擎为纯函数：不读取数据库、不修改入参，回放不产生任何持久化。
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from app.compat import (
    escape_pointer,
    make_validator,
    resolve_refs,
)
from app.profile import (
    MAX_EXAMPLE_INDICES,
    failure_fingerprint,
    render_instance_pointer,
    resolve_error,
)

# 单次回放允许的最大文档数（与 schemas.ReplayRequest 保持一致）。
MAX_REPLAY_DOCUMENTS = 5000

# ---------------------------------------------------------------------------
# jsonschema 规则名 -> 可能对应的报告 issue 代码
# ---------------------------------------------------------------------------
# 多数规则在一次收紧变更中只有一个代码；类型与组合规则可能对应多种
# 结构性变化，按列表顺序依次尝试匹配。
_VALIDATOR_CODES: dict[str, list[str]] = {
    "required": ["required_added"],
    "enum": ["enum_value_removed", "enum_added"],
    "const": ["const_added", "const_changed"],
    "type": ["type_removed", "type_restricted", "integer_required"],
    "minimum": ["minimum_increased"],
    "maximum": ["maximum_decreased"],
    "exclusiveMinimum": ["minimum_increased"],
    "exclusiveMaximum": ["maximum_decreased"],
    "multipleOf": ["multiple_of_added"],
    "minLength": ["min_length_increased"],
    "maxLength": ["max_length_decreased"],
    "pattern": ["pattern_changed"],
    "minItems": ["min_items_increased"],
    "maxItems": ["max_items_decreased"],
    "additionalItems": ["max_items_decreased", "schema_forbids_all"],
    "items": ["schema_forbids_all"],
    "uniqueItems": ["unique_items_added"],
    "minProperties": ["min_properties_increased"],
    "maxProperties": ["max_properties_decreased"],
    "additionalProperties": [
        "additional_properties_forbidden",
        "property_no_longer_allowed",
    ],
    "not": ["not_added"],
    "oneOf": ["oneOf_branch_missing", "oneOf_added", "oneOf_branch_rejected"],
    "anyOf": ["anyOf_branch_missing", "anyOf_added", "anyOf_branch_rejected"],
}

# 这些 schema 关键字是"附加在所属节点上的约束"，规则位置退一段即得到
# 受约束节点指针；不在表中的（如 items / additionalProperties / false
# 子 schema）由 _node_pointers 特判处理。
_NODE_KEYWORDS = {
    "required", "enum", "const", "type",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties",
    "not", "oneOf", "anyOf", "allOf",
}


def render_pointer(parts: list[Any]) -> str:
    rendered = [escape_pointer(str(p)) for p in parts]
    return "#/" + "/".join(rendered) if rendered else "#"


def _tuple_alias(pointer: str) -> str | None:
    """把列表形式 items 的元组路径别名改写为 prefixItems 风格（反向亦然）。

    报告路径经 ``resolve_tuple_placeholders`` 渲染：draft-04/06/07 列表
    items 渲染成 ``#/items/0``，2019-09+ 渲染成 ``#/prefixItems/0``。
    jsonschema 的 ``absolute_schema_path`` 按实际关键字给路径，因此回放
    时两种写法都要尝试。
    """
    if pointer.startswith("#/items/"):
        return "#/prefixItems/" + pointer[len("#/items/"):]
    if pointer.startswith("#/prefixItems/"):
        return "#/items/" + pointer[len("#/prefixItems/"):]
    return None


def _walk_schema_node(schema: Any, parts: list[Any]) -> Any:
    """按 schema_path 片段走到子 schema（解析 $ref 后的宽松导航）。"""
    node = schema
    for part in parts:
        if isinstance(node, dict):
            if part in node:
                node = node[part]
            elif isinstance(node.get("items"), list) and isinstance(part, int):
                node = node["items"][part]
            else:
                return None
        elif isinstance(node, list) and isinstance(part, int) and part < len(node):
            node = node[part]
        else:
            return None
    return node


def _node_pointers(error: Any, failing_schema: Any = None) -> list[str]:
    """推导一条校验错误的受约束 schema 节点候选指针（有序、去重）。

    ``failing_schema`` 为解析 $ref 后的校验侧 schema，用于定位布尔
    ``false`` 子 schema（jsonschema 对这类错误只报父关键字，不给属性名）。
    """
    schema_path = list(error.absolute_schema_path)
    validator = error.validator
    pointers: list[str] = []

    def add(parts: list[Any]) -> None:
        pointer = render_pointer(list(parts))
        if pointer not in pointers:
            pointers.append(pointer)
        alias = _tuple_alias(pointer)
        if alias and alias not in pointers:
            pointers.append(alias)

    if validator is None:
        # 子 schema 为 false：路径停在父关键字（properties / items …），
        # 校验器不报告具体属性名，需要在 schema 中定位 false 子节点。
        parent_path = schema_path[:-1] if schema_path else []
        parent = _walk_schema_node(failing_schema, parent_path) if failing_schema is not None else None
        last = schema_path[-1] if schema_path else None
        if last == "properties" and isinstance(parent, dict):
            props = parent.get("properties")
            if isinstance(props, dict):
                for name, sub in props.items():
                    if sub is False:
                        add(parent_path + ["properties", name])
            if not pointers:
                add(schema_path)
        elif last == "items":
            add(parent_path + ["items"])
        elif last == "additionalProperties":
            add(parent_path + ["additionalProperties"])
        else:
            add(schema_path)
        return pointers

    if validator in _NODE_KEYWORDS:
        add(schema_path[:-1])
        return pointers

    if validator in ("additionalProperties", "propertyNames"):
        # 关键字节点本身（false AP / propertyNames 收紧）与其所属对象节点
        add(schema_path if schema_path and schema_path[-1] == validator
            else schema_path + [validator])
        add(schema_path[:-1])
        return pointers

    if validator == "additionalItems":
        add(["items"])
        add(schema_path[:-1])
        return pointers

    if validator == "items":
        # items:false（draft7 / 2020-12）触发的裸 items 规则：受约束节点
        # 是 #/items；深层元素错误的 schema_path 已带具体关键字，不会走到这。
        add(schema_path)
        add(schema_path[:-1])
        return pointers

    # 未知规则：原样使用 schema 位置
    add(schema_path)
    return pointers


def _instance_pointer(error: Any) -> str:
    return render_instance_pointer(list(error.absolute_path))


def _attributable_codes(validator: str | None) -> list[str]:
    if validator is None:
        return ["schema_forbids_all"]
    return list(_VALIDATOR_CODES.get(str(validator), []))


# ---------------------------------------------------------------------------
# 单文档回放
# ---------------------------------------------------------------------------


def replay_document(document: Any, old_validator, new_validator) -> dict:
    """用旧 / 新校验器校验单个文档，返回方向与两侧错误。"""
    old_errors = list(old_validator.iter_errors(document))
    new_errors = list(new_validator.iter_errors(document))
    old_ok, new_ok = not old_errors, not new_errors
    if old_ok and not new_ok:
        direction = "backward"
    elif new_ok and not old_ok:
        direction = "forward"
    else:
        direction = None
    return {
        "valid_against_old": old_ok,
        "valid_against_new": new_ok,
        "direction": direction,
        "backward_errors": new_errors if direction == "backward" else [],
        "forward_errors": old_errors if direction == "forward" else [],
        "both_invalid_errors": (old_errors + new_errors) if direction is None
        and not (old_ok and new_ok) else [],
    }


# ---------------------------------------------------------------------------
# 归因
# ---------------------------------------------------------------------------


def _build_issue_indexes(report: dict) -> tuple[dict, dict]:
    """构造 (path, code) -> issue 摘要 与 样例 issue 的 path 索引。

    同一 (path, code) 在报告中可能重复出现（如不同 reason 的同码问题），
    归因时命中第一个（报告按 direction/path/code 排序，选择是确定的）。
    """
    structural: dict[tuple[str, str, str], dict] = {}
    sample: dict[tuple[str, str], list[dict]] = {}
    for issue in report.get("issues", []):
        direction = issue.get("direction")
        path = issue.get("path")
        code = issue.get("code")
        entry = {
            "direction": direction,
            "path": path,
            "code": code,
            "severity": issue.get("severity"),
            "message": issue.get("message"),
        }
        key = (direction, path, code)
        structural.setdefault(key, entry)
        if code in ("sample_rejected_by_new", "sample_rejected_by_old"):
            sample.setdefault((direction, path), []).append(entry)
    return structural, sample


def attribute_error(
    error: Any,
    direction: str,
    structural: dict,
    sample: dict,
    failing_schema: Any = None,
) -> dict | None:
    """把一条 jsonschema 错误归因到报告 issue；无法归因返回 None。"""
    # 1) 结构性归因：受约束节点指针 × 候选 issue 代码
    for pointer in _node_pointers(error, failing_schema):
        for code in _attributable_codes(error.validator):
            issue = structural.get((direction, pointer, code))
            if issue is not None:
                return issue
    # 2) 样例 issue 归因：同方向、同实例位置（样例 issue 以实例位置生成）
    inst_pointer = _instance_pointer(error)
    candidates = sample.get((direction, inst_pointer))
    if candidates:
        return candidates[0]
    return None


# ---------------------------------------------------------------------------
# 顶层入口
# ---------------------------------------------------------------------------


def replay_samples(
    old_schema: Any,
    new_schema: Any,
    report: dict,
    documents: list[Any],
) -> dict:
    """对 documents 做样本回放并归因到报告 issue（纯函数）。

    ``documents`` 由调用方保证为 1..MAX_REPLAY_DOCUMENTS 个 JSON 文档。
    """
    old_resolved = resolve_refs(old_schema)
    new_resolved = resolve_refs(new_schema)
    old_validator = make_validator(old_resolved)
    new_validator = make_validator(new_resolved)
    structural, sample = _build_issue_indexes(report)

    issue_order: list[tuple[str, str, str]] = [
        (issue.get("direction"), issue.get("path"), issue.get("code"))
        for issue in report.get("issues", [])
    ]
    hits: "OrderedDict[tuple, dict]" = OrderedDict()
    unattributed: "OrderedDict[tuple, dict]" = OrderedDict()

    counts = {"total": len(documents), "valid_both": 0, "invalid_both": 0,
              "backward": 0, "forward": 0}
    results: list[dict] = []

    def remember_hit(issue: dict, index: int) -> None:
        key = (issue["direction"], issue["path"], issue["code"])
        group = hits.get(key)
        if group is None:
            hits[key] = {
                "direction": issue["direction"],
                "path": issue["path"],
                "code": issue["code"],
                "severity": issue["severity"],
                "message": issue["message"],
                "hit_count": 1,
                "example_indices": [index],
            }
        else:
            group["hit_count"] += 1
            if len(group["example_indices"]) < MAX_EXAMPLE_INDICES:
                group["example_indices"].append(index)

    def remember_unattributed(group_key: tuple, direction: str | None,
                              index: int, record: dict) -> None:
        group = unattributed.get(group_key)
        if group is None:
            unattributed[group_key] = {
                "fingerprint": group_key[1],
                "direction": direction,
                "pointer": record["pointer"],
                "validator": record["validator"],
                "schema_pointer": record["schema_pointer"],
                "message": record["message"],
                "count": 1,
                "example_indices": [index],
            }
        else:
            group["count"] += 1
            if len(group["example_indices"]) < MAX_EXAMPLE_INDICES:
                group["example_indices"].append(index)
            if record["schema_pointer"] < group["schema_pointer"]:
                group["schema_pointer"] = record["schema_pointer"]
            if record["message"] < group["message"]:
                group["message"] = record["message"]

    for index, document in enumerate(documents):
        outcome = replay_document(document, old_validator, new_validator)
        direction = outcome["direction"]
        if direction == "backward":
            counts["backward"] += 1
            errors = outcome["backward_errors"]
        elif direction == "forward":
            counts["forward"] += 1
            errors = outcome["forward_errors"]
        elif outcome["valid_against_old"] and outcome["valid_against_new"]:
            counts["valid_both"] += 1
            errors = []
        else:
            counts["invalid_both"] += 1
            errors = outcome["both_invalid_errors"]

        attributed_keys: set[tuple] = set()
        unattributed_this_doc: list[dict] = []
        seen_unattributed: set[tuple] = set()
        for error in errors:
            issue = (
                attribute_error(error, direction, structural, sample,
                                new_resolved if direction == "backward"
                                else old_resolved)
                if direction is not None else None
            )
            if issue is not None:
                key = (issue["direction"], issue["path"], issue["code"])
                if key not in attributed_keys:
                    attributed_keys.add(key)
                    remember_hit(issue, index)
            else:
                record = resolve_error(error)
                fingerprint = failure_fingerprint(record)
                group_key = (direction, fingerprint)
                if group_key in seen_unattributed:
                    # 两版均拒绝的文档会同时产生旧 / 新两侧错误，同组
                    # 在一个文档内只保留一次（与画像的按文档去重一致）。
                    continue
                seen_unattributed.add(group_key)
                unattributed_this_doc.append({
                    "pointer": record["pointer"],
                    "validator": record["validator"],
                    "message": record["message"],
                })
                remember_unattributed(group_key, direction, index, record)

        results.append({
            "index": index,
            "valid_against_old": outcome["valid_against_old"],
            "valid_against_new": outcome["valid_against_new"],
            "direction": direction,
            "attributed_issue_count": len(attributed_keys),
            "unattributed_error_count": len(unattributed_this_doc),
        })

    # 命中组按报告内 issue 原始顺序输出
    issue_hits = [
        hits[key] for key in issue_order if key in hits
    ]
    unattributed_failures = sorted(
        unattributed.values(),
        key=lambda g: (str(g["direction"] or ""), g["pointer"],
                       g["validator"], g["schema_pointer"], g["fingerprint"]),
    )
    return {
        "total_documents": counts["total"],
        "valid_documents": counts["valid_both"],
        "backward_failures": counts["backward"],
        "forward_failures": counts["forward"],
        "invalid_under_both": counts["invalid_both"],
        "issue_hits": issue_hits,
        "unattributed_failures": unattributed_failures,
        "document_results": results,
    }
