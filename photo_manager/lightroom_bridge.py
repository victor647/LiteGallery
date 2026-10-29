from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .paths import bridge_dir


class LightroomBridge:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or bridge_dir()
        self.requests_dir = self.root / "requests"
        self.acks_dir = self.root / "acks"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.acks_dir.mkdir(parents=True, exist_ok=True)

    def enqueue(
        self,
        action: str,
        path: Path,
        operation_id: str | None = None,
        output_path: Path | None = None,
    ) -> str:
        if action not in {"import", "remove", "render"}:
            raise ValueError("Lightroom 操作无效。")
        operation_id = operation_id or uuid.uuid4().hex
        target = self.requests_dir / f"{operation_id}.json"
        acknowledgement = self.acks_dir / f"{operation_id}.json"
        if target.exists() or acknowledgement.exists():
            return operation_id
        request = {
            "version": 1,
            "operation_id": operation_id,
            "action": action,
            "path": str(path.resolve()),
        }
        if output_path is not None:
            request["output_path"] = str(output_path.resolve())
        temporary = self.requests_dir / f".{operation_id}.tmp"
        temporary.write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, target)
        return operation_id

    def pending_acks(self) -> list[tuple[Path, dict[str, Any]]]:
        result: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.acks_dir.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("operation_id"):
                    result.append((path, value))
            except (OSError, json.JSONDecodeError):
                continue
        return result

    @staticmethod
    def acknowledge(path: Path) -> None:
        path.unlink(missing_ok=True)
