"""值班命令行入口。

子命令：
  init       用清册初始化值班状态目录
  ingest     接入新的校时观测、申请或执行回执（JSONL 文件或单条 JSON）
  ops        查询操作判定与生命周期
  partitions 查询各分区可信时间与隔离状态
  seats      查询席位有效使用权、过期状态与受影响请求
  explain    展开单个操作的证据准入与判定理由
  advance    实际推进一个已通过的续用/回收/领取
  recover    恢复欠账：dry-run 给方案，--apply 实际处理
  run        连续演示：一次跑完整个事件文件，打印每个阶段的处置
  verify     用日志重放核对快照，证明重启后旧决定与证据可查证
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import DutyEngine
from .model import OperationStatus
from .store import DutyStore
from .validation import RecordError


def _load_json_arg(value: str) -> dict:
    path = Path(value)
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(value)


def _load_records(value: str) -> list[dict]:
    path = Path(value)
    if path.exists():
        records = []
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise RecordError(f"{path.name}:{number} 不是合法 JSON：{error}") from error
        return records
    return [json.loads(value)]


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _open_engine(args) -> DutyEngine:
    store = DutyStore(args.state)
    if not store.has_inventory():
        raise SystemExit(f"状态目录 {args.state} 尚未 init，请先指定清册初始化")
    return DutyEngine.open(store)


def cmd_init(args) -> None:
    inventory = _load_json_arg(args.inventory)
    store = DutyStore(args.state)
    if store.has_inventory():
        raise SystemExit(f"状态目录 {args.state} 已初始化")
    engine = DutyEngine(inventory)
    engine.attach_store(store)
    _print_json({"initialized": args.state, "seats": [s["seat"] for s in inventory["seats"]]})


def cmd_ingest(args) -> None:
    engine = _open_engine(args)
    records = _load_records(args.source)
    try:
        results = engine.ingest_file(records)
    except RecordError as error:
        raise SystemExit(f"接入失败：{error}") from error
    if args.json:
        _print_json(results)
        return
    for item in results:
        if item["kind"] == "observation":
            print(f"t={item['at']} 观测已登记 来源={item['observation']['source']} "
                  f"分区={item['observation']['partition']}")
        elif item["kind"] == "boot":
            print(f"t={item['at']} 主机 {item['host']} 重启，boot={item['boot']}，旧观测作废")
        elif item["kind"] == "intent":
            d = item["decision"]
            print(f"t={item['at']} 申请 {d['operation']}（{d['action']}）=> {d['status']}"
                  f" [{d['verdict']}] {'; '.join(d['reasons'])}")
        else:
            extra = []
            if "anomaly" in item:
                extra.append(f"异常：{item['anomaly']}")
            if "outcome" in item:
                extra.append(f"outcome={item['outcome']} applied={item['applied']} "
                             f"superseded={item['superseded']}")
            print(f"t={item['at']} 回执 {item['operation']} {' '.join(extra)}")


def cmd_ops(args) -> None:
    engine = _open_engine(args)
    status = OperationStatus(args.status) if args.status else None
    ops = engine.list_operations(status)
    if args.json:
        _print_json([op.to_dict() for op in ops])
        return
    if not ops:
        print("（无匹配操作）")
    for op in ops:
        line = (f"{op.operation} seat={op.seat} gen={op.generation} {op.action.value} "
                f"holder={op.holder} status={op.status.value}")
        if op.verdict:
            line += f" verdict={op.verdict.value}"
        if op.outcome:
            line += f" outcome={op.outcome.value}"
        print(line)
        for reason in op.reasons:
            print(f"    - {reason}")
        if op.recovery_note:
            print(f"    恢复备注：{op.recovery_note}")


def cmd_partitions(args) -> None:
    engine = _open_engine(args)
    _print_json(engine.partition_status())


def cmd_seats(args) -> None:
    engine = _open_engine(args)
    _print_json(engine.seat_status())


def cmd_explain(args) -> None:
    engine = _open_engine(args)
    try:
        _print_json(engine.explain(args.operation))
    except RecordError as error:
        raise SystemExit(str(error)) from error


def cmd_advance(args) -> None:
    engine = _open_engine(args)
    try:
        op = engine.advance(args.operation)
    except RecordError as error:
        raise SystemExit(f"推进失败：{error}") from error
    _print_json({"advanced": op.operation, "status": op.status.value, "at": op.advanced_at})


def cmd_recover(args) -> None:
    engine = _open_engine(args)
    plan = engine.recover(apply=args.apply)
    mode = "apply" if args.apply else "dry-run"
    if not args.json:
        print(f"恢复方案（{mode}，t={engine.last_at}）：")
        for bucket in ("redrive", "approve", "review", "waiting"):
            labels = {
                "redrive": "可安全补做/重派",
                "approve": "现已通过、可推进",
                "review": "留值班复核",
                "waiting": "继续等待",
            }
            print(f"  [{labels[bucket]}]")
            for item in plan[bucket]:
                print(f"    - {item['operation']}（{item.get('action', '')}）：{item['reason']}")
    _print_json({"mode": mode, "plan": plan}) if args.json else None


def cmd_verify(args) -> None:
    engine = _open_engine(args)
    diffs = engine.verify_journal()
    if diffs:
        _print_json({"consistent": False, "diffs": diffs})
        raise SystemExit(1)
    _print_json({"consistent": True,
                 "journal_records": len(engine.store.read_journal()),
                 "operations": len(engine.operations),
                 "observations": len(engine.observations)})


def cmd_run(args) -> None:
    """连续操作演示：接入资料 -> 推进可通过项 -> 恢复欠账。"""
    store = DutyStore(args.state)
    if store.has_inventory():
        engine = DutyEngine.open(store)
        print(f"（状态目录已存在，继续接入；当前 t={engine.last_at}）")
    else:
        inventory = _load_json_arg(args.inventory)
        engine = DutyEngine(inventory)
        engine.attach_store(store)

    records = _load_records(args.source)
    print(f"== 连续接入 {len(records)} 条资料 ==")
    for record in records:
        result = engine.ingest_record(record)
        if record["kind"] == "intent":
            d = result["decision"]
            print(f"  t={result['at']} {d['operation']}({d['action']}) => "
                  f"{d['status']} [{d['verdict']}] {'; '.join(d['reasons'])}")
        elif record["kind"] == "boot":
            print(f"  t={result['at']} 主机 {result['host']} 重启（boot={result['boot']}）")
        elif record["kind"] == "effect":
            print(f"  t={result['at']} 回执 {result['operation']}："
                  f"{result.get('outcome', result.get('anomaly', ''))} "
                  f"applied={result.get('applied')} superseded={result.get('superseded')}")
        else:
            print(f"  t={result['at']} 观测 {result['observation']['source']} 已登记")

    print("== 值班推进所有已通过操作 ==")
    advanced_any = False
    for op in engine.list_operations(OperationStatus.APPROVED):
        engine.advance(op.operation)
        advanced_any = True
        print(f"  推进 {op.operation}（{op.action.value}）")
    if not advanced_any:
        print("  （当前无 approved 操作）")

    print("== 恢复欠账（dry-run） ==")
    plan = engine.recover(apply=False)
    for bucket in ("redrive", "approve", "review", "waiting"):
        for item in plan[bucket]:
            print(f"  {bucket}: {item['operation']} - {item['reason']}")
    if not any(plan.values()):
        print("  （无欠账）")

    print("== 终态席位与分区 ==")
    _print_json({"seats": engine.seat_status(), "partitions": engine.partition_status()})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校时故障值班能力")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="用清册初始化状态目录")
    p.add_argument("inventory", help="inventory.json 路径或内联 JSON")
    p.add_argument("--state", required=True, help="值班状态目录")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ingest", help="接入观测/申请/回执")
    p.add_argument("source", help="JSONL 文件路径或单条 JSON 字符串")
    p.add_argument("--state", required=True)
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("ops", help="查询操作")
    p.add_argument("--state", required=True)
    p.add_argument("--status", choices=[s.value for s in OperationStatus])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ops)

    p = sub.add_parser("partitions", help="查询分区可信时间与隔离")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_partitions)

    p = sub.add_parser("seats", help="查询席位使用权与过期状态")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_seats)

    p = sub.add_parser("explain", help="展开单个操作的证据与理由")
    p.add_argument("operation")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("advance", help="推进一个已通过操作")
    p.add_argument("operation")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_advance)

    p = sub.add_parser("recover", help="恢复欠账")
    p.add_argument("--state", required=True)
    p.add_argument("--apply", action="store_true", help="实际执行方案（默认 dry-run）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("verify", help="日志重放核对快照")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("run", help="连续演示整个事件文件")
    p.add_argument("inventory", help="inventory.json 路径或内联 JSON")
    p.add_argument("source", help="JSONL 事件文件")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (OSError, RecordError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
