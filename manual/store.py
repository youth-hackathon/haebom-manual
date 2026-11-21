"""매뉴얼 파일. 매뉴얼 하나가 JSON 파일 하나다 (manuals/<id>.json).

    {
      "id": "bluetooth",
      "task": "설정에서 블루투스 화면 열어줘",       이 매뉴얼이 맡는 요청. 저작할 때 쓴 문장이다
      "aliases": ["블루투스 화면 열어줘"],            같은 일로 보는 다른 문장
      "status": "draft" | "verified",                앞단 서버는 verified만 쓴다
      "steps": [{"id", "seq", "device_model", "instruction", "screen_condition", "done_condition"}],
      "verified": {...}                               저작 서버가 남긴 검증 기록
    }

단계의 순서(seq)가 뜻을 가진다: 지금 화면에 맞는 단계가 여럿이면 가장 뒤의 것을 안내한다(rules.find).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def norm_task(task: str) -> str:
    """The request as the person wrote it: its first paragraph, without spaces, case or closing punctuation.

    The app adds a note for the planner after a blank line; that is not part of what the person asked.
    """
    first = str(task).split("\n\n", 1)[0]
    return re.sub(r"[\s.!?~。]+", "", first).casefold()


class Manuals:
    def __init__(self, folder: str | os.PathLike):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self._stamp: tuple | None = None
        self.reload()

    # ---------------------------------------------------------------- files

    def _scan(self) -> tuple:
        return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in self.folder.glob("*.json")))

    def reload(self) -> None:
        """Read the folder again when a file changed (the authoring server publishes into it while this one runs)."""
        with self._lock:
            stamp = self._scan()
            if stamp == self._stamp:
                return
            items: dict[str, dict] = {}
            for p in sorted(self.folder.glob("*.json")):
                try:
                    m = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    print(f"[manuals] {p.name} is not read: {exc}", flush=True)
                    continue
                if isinstance(m, dict) and m.get("id"):
                    m.setdefault("aliases", [])
                    m.setdefault("steps", [])
                    m.setdefault("status", "draft")
                    items[str(m["id"])] = m
            self._items, self._stamp = items, stamp

    def save(self, m: dict) -> None:
        with self._lock:
            m["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._items[str(m["id"])] = m
            path = self.folder / f"{m['id']}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            self._stamp = self._scan()

    # ---------------------------------------------------------------- manuals

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._items.values())

    def get(self, mid: str) -> dict | None:
        with self._lock:
            return self._items.get(str(mid))

    def match(self, task: str, verified_only: bool = True) -> dict | None:
        """The manual written for this request: the same sentence, or one of its aliases. Never a near miss."""
        want = norm_task(task)
        if not want:
            return None
        with self._lock:
            for m in self._items.values():
                if verified_only and m.get("status") != "verified":
                    continue
                if want in {norm_task(t) for t in [m.get("task", ""), *m.get("aliases", [])]}:
                    return m
        return None

    def create(self, task: str, mid: str | None = None) -> dict:
        with self._lock:
            if mid is None:
                while True:
                    mid = "".join(secrets.choice(_ALPHABET) for _ in range(8))
                    if mid not in self._items:
                        break
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", mid):
                raise ValueError(f"manual id {mid!r}: letters, digits, - and _ only")
            if mid in self._items:
                raise ValueError(f"manual {mid} already exists")
            m = {"id": mid, "task": str(task).split("\n\n", 1)[0].strip(), "aliases": [], "status": "draft", "steps": [],
                 "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self.save(m)
            return m

    # ---------------------------------------------------------------- steps

    def steps(self, mid: str) -> list[dict]:
        m = self.get(mid)
        return sorted(m["steps"], key=lambda s: (s["seq"], s["id"])) if m else []

    def step_seq(self, mid: str, step_id: int) -> int | None:
        """Where a step stands now (steps saved before it move it down)."""
        return next((s["seq"] for s in self.steps(mid) if s["id"] == step_id), None)

    def reserve_seq(self, mid: str, device_model: str, after_seq: int) -> int:
        """The place right after `after_seq` for a step about to be saved; later steps of this device move down by one."""
        seq = after_seq + 1
        with self._lock:
            m = self._items[str(mid)]
            for s in m["steps"]:
                if s.get("device_model") == device_model and s["seq"] >= seq:
                    s["seq"] += 1
        return seq

    def add_step(self, mid: str, device_model: str, seq: int, step: dict) -> int | None:
        """Save a step unless this device already has the same condition and action. Returns its id."""
        with self._lock:
            m = self._items[str(mid)]
            key = _key(step)
            if any(s.get("device_model") == device_model and _key(s) == key for s in m["steps"]):
                return None
            sid = max((s["id"] for s in m["steps"]), default=0) + 1
            m["steps"].append({"id": sid, "seq": seq, "device_model": device_model, "instruction": step["instruction"],
                               "screen_condition": step["screen_condition"], "done_condition": step["done_condition"]})
            m["steps"].sort(key=lambda s: (s["seq"], s["id"]))
            self.save(m)
            return sid

    def delete_step(self, mid: str, step_id: int) -> None:
        with self._lock:
            m = self._items.get(str(mid))
            if m is None:
                return
            m["steps"] = [s for s in m["steps"] if s["id"] != step_id]
            self.save(m)

    def forget(self, mid: str, device_model: str | None = None) -> int:
        """Drop the steps of a manual (of one device, or all). Returns how many were dropped."""
        with self._lock:
            m = self._items.get(str(mid))
            if m is None:
                return 0
            keep = [s for s in m["steps"] if device_model is not None and s.get("device_model") != device_model]
            dropped = len(m["steps"]) - len(keep)
            m["steps"] = keep
            m["status"] = "draft"
            self.save(m)
            return dropped


def _key(step: dict) -> tuple[str, str]:
    return (json.dumps(step["screen_condition"], ensure_ascii=False, sort_keys=True),
            json.dumps(step["done_condition"], ensure_ascii=False, sort_keys=True))
