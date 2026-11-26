"""值班状态持久化。

状态目录包含：
- ``inventory.json``：首次初始化时固化的清册；
- ``journal.jsonl``：追加写入的全部事件与值班动作（含校验异常不入日志）；
- ``snapshot.json``：每次写入后刷新的完整状态快照，重启时直接载入，
  日志仍保留用于逐条查证旧的席位决定和证据。

只使用标准库，原子替换快照文件，避免中途停止服务造成半写状态。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .model import Observation, Seat


class DutyStore:
    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.directory / "journal.jsonl"
        self.snapshot_path = self.directory / "snapshot.json"
        self.inventory_path = self.directory / "inventory.json"

    # ---- 清册 ----------------------------------------------------------
    def save_inventory(self, inventory: dict) -> None:
        if self.inventory_path.exists():
            return
        self._atomic_text(self.inventory_path, json.dumps(inventory, ensure_ascii=False, indent=2))

    def load_inventory(self) -> dict:
        with self.inventory_path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def has_inventory(self) -> bool:
        return self.inventory_path.exists()

    # ---- 日志 ----------------------------------------------------------
    def append_journal(self, entry: dict) -> None:
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_journal(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        entries = []
        with self.journal_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    # ---- 快照 ----------------------------------------------------------
    def save_snapshot(self, state: dict) -> None:
        self._atomic_text(
            self.snapshot_path,
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        )

    def load_snapshot(self) -> dict | None:
        if not self.snapshot_path.exists():
            return None
        with self.snapshot_path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _atomic_text(self, path: Path, text: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)


def serialize_observation(obs: Observation) -> dict:
    return {
        "source": obs.source,
        "partition": obs.partition,
        "host": obs.host,
        "boot": obs.boot,
        "ticks": obs.ticks,
        "action_time": obs.action_time,
        "bounds": list(obs.bounds),
        "received_at": obs.received_at,
    }


def deserialize_observation(data: dict) -> Observation:
    return Observation(
        source=data["source"],
        partition=data["partition"],
        host=data["host"],
        boot=data["boot"],
        ticks=data["ticks"],
        action_time=data["action_time"],
        bounds=(data["bounds"][0], data["bounds"][1]),
        received_at=data["received_at"],
    )


def serialize_seat(seat: Seat) -> dict:
    return {
        "seat": seat.seat,
        "partition": seat.partition,
        "holder": seat.holder,
        "generation": seat.generation,
        "expires": seat.expires,
    }


def deserialize_seat(data: dict) -> Seat:
    return Seat(**data)
