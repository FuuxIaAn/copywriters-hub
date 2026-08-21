# -*- coding: utf-8 -*-
"""Repair verified historical speaker-label errors without an LLM call."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACT = ROOT / "output" / "extract"
BACKUP = EXTRACT / f"repair_backup_{time.strftime('%Y%m%d_%H%M%S')}"
LABELS = ["B", "A", "B", "A", "B", "A", "A", "B", "B", "A", "A", "B", "B", "A"]


def main() -> int:
    changed = []
    for path in EXTRACT.rglob("*.json"):
        if BACKUP in path.parents or path.name == "latest.json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        segments = record.get("segments") or []
        text = (record.get("raw_text") or "").strip()
        if len(segments) != len(LABELS) or "师傅你好，我想找您算一卦" not in text:
            continue
        source = [str(s.get("text") or "").strip() for s in segments]
        if not all(source) or "你想问什么" not in source[1] or "真的吗" not in source[8]:
            continue
        BACKUP.mkdir(parents=True, exist_ok=True)
        target_backup = BACKUP / path.relative_to(EXTRACT)
        target_backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target_backup)
        for seg, speaker in zip(segments, LABELS):
            seg["speaker"] = speaker
        record["segments"] = segments
        record["repair_note"] = "历史双人对话 speaker 标签修复：按问答角色与原始句序校正"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        changed.append(str(path))
    print(json.dumps({"changed": changed, "backup": str(BACKUP) if changed else ""}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
