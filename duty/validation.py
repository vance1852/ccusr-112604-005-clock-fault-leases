"""合成资料校验：清册与事件记录。"""

from __future__ import annotations

from .model import Action, Outcome

_RECORD_KINDS = {"observation", "boot", "intent", "effect"}
_ACTIONS = {a.value for a in Action}
_OUTCOMES = {o.value for o in Outcome}


class RecordError(ValueError):
    """资料格式或字段不合法。"""


def require_int(obj: dict, key: str, *, nonneg: bool = False) -> int:
    value = obj.get(key)
    if type(value) is not int or isinstance(value, bool):
        raise RecordError(f"字段 {key} 应为整数")
    if nonneg and value < 0:
        raise RecordError(f"字段 {key} 应为非负整数")
    return value


def validate_inventory(inventory: dict) -> None:
    for key in ("sources", "partitions", "seats"):
        if not isinstance(inventory.get(key), list) or not inventory[key]:
            raise RecordError(f"清册 {key} 应为非空列表")
    for entry in inventory["sources"]:
        if not isinstance(entry, dict) or not entry.get("source") or not entry.get("domain"):
            raise RecordError("时间源需要 source 与 domain")
    names = [s["source"] for s in inventory["sources"]]
    if len(names) != len(set(names)):
        raise RecordError("时间源名称重复")
    for entry in inventory["partitions"]:
        if not isinstance(entry, dict) or not entry.get("partition"):
            raise RecordError("分区需要 partition 名称")
        require_int(entry, "source_domains_min")
        if entry["source_domains_min"] < 1:
            raise RecordError("source_domains_min 至少为 1")
        require_int(entry, "freshness")
        if entry["freshness"] < 1:
            raise RecordError("freshness 至少为 1")
    pnames = [p["partition"] for p in inventory["partitions"]]
    if len(pnames) != len(set(pnames)):
        raise RecordError("分区名称重复")
    for entry in inventory["seats"]:
        if not isinstance(entry, dict) or not entry.get("seat"):
            raise RecordError("席位需要 seat 名称")
        if entry.get("partition") not in pnames:
            raise RecordError(f"席位 {entry['seat']} 引用未知分区")
        require_int(entry, "generation", nonneg=True)
        require_int(entry, "expires")
        if not entry.get("holder"):
            raise RecordError(f"席位 {entry['seat']} 缺少 holder")
    snames = [s["seat"] for s in inventory["seats"]]
    if len(snames) != len(set(snames)):
        raise RecordError("席位名称重复")


def validate_record(record: dict) -> None:
    kind = record.get("kind")
    if kind not in _RECORD_KINDS:
        raise RecordError(f"未知事件类别 {kind!r}")
    require_int(record, "at", nonneg=True)
    if kind == "observation":
        for key in ("source", "partition", "host"):
            if not isinstance(record.get(key), str) or not record[key]:
                raise RecordError(f"observation 缺少 {key}")
        require_int(record, "boot", nonneg=True)
        require_int(record, "ticks", nonneg=True)
        require_int(record, "action_time", nonneg=True)
        bounds = record.get("bounds")
        if (not isinstance(bounds, list) or len(bounds) != 2
                or any(type(v) is not int for v in bounds)):
            raise RecordError("observation 的 bounds 应为两个整数")
        if bounds[0] > bounds[1]:
            raise RecordError("observation 的 bounds 下界不能大于上界")
    elif kind == "boot":
        if not isinstance(record.get("host"), str) or not record["host"]:
            raise RecordError("boot 缺少 host")
        require_int(record, "boot", nonneg=True)
    elif kind == "intent":
        for key in ("operation", "seat", "holder"):
            if not isinstance(record.get(key), str) or not record[key]:
                raise RecordError(f"intent 缺少 {key}")
        if record.get("action") not in _ACTIONS:
            raise RecordError("intent 的 action 非法")
        require_int(record, "generation", nonneg=True)
        expiry = record.get("requested_expiry")
        if expiry is not None and type(expiry) is not int:
            raise RecordError("requested_expiry 应为整数或 null")
    else:  # effect
        for key in ("operation", "seat"):
            if not isinstance(record.get(key), str) or not record[key]:
                raise RecordError(f"effect 缺少 {key}")
        require_int(record, "generation", nonneg=True)
        if record.get("outcome") not in _OUTCOMES:
            raise RecordError("effect 的 outcome 非法")
