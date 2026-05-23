# <!-- CDD-CONTRACT: ~/gbrain/skills/linkedin-deepen/_shared/idempotency.py -->
# Contract:   per-(page-path, section, source-row-hash) applied-registry.
# Inputs:     hash key (string) for each potential write.
# Outputs:    bool was_applied / mark_applied(key); commit_audit() writes
#             the registry to disk.
# Invariants: registry persists across runs; re-run on unchanged source
#             hits idempotent-skip for every previously-applied key.
#             Thread-safe: internal lock protects _keys / _dirty / counter
#             mutations so concurrent mark_applied is safe under
#             max_workers > 1.
# Idempotent: yes. auto_flush_every=N triggers atomic-rename commit after
#             every N marks (kill-mid-run safety per S191-D119+2 patch).

"""Per-(page, section, row-hash) applied-registry for linkedin-deepen.

Registry file: <audit-dir>/applied.json
Format: {"keys": ["<sha16>", ...]}
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Set


class AppliedRegistry:
    def __init__(self, path: Path, auto_flush_every: int = 0):
        self.path = path
        self.auto_flush_every = max(0, int(auto_flush_every))
        self._keys: Set[str] = set()
        self._dirty: bool = False
        self._unflushed: int = 0
        self._lock = threading.Lock()
        self.auto_flush_count: int = 0
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for k in data.get("keys", []):
                if isinstance(k, str):
                    self._keys.add(k)
        except (OSError, json.JSONDecodeError):
            return

    def was_applied(self, key: str) -> bool:
        with self._lock:
            return key in self._keys

    def mark_applied(self, key: str) -> None:
        should_flush = False
        with self._lock:
            if not key or key in self._keys:
                return
            self._keys.add(key)
            self._dirty = True
            self._unflushed += 1
            if (
                self.auto_flush_every > 0
                and self._unflushed >= self.auto_flush_every
            ):
                should_flush = True
        if should_flush:
            self._do_commit(auto=True)

    def commit_audit(self) -> None:
        self._do_commit(auto=False)

    def _do_commit(self, *, auto: bool) -> None:
        with self._lock:
            if not self._dirty:
                return
            snapshot = sorted(self._keys)
            self._dirty = False
            self._unflushed = 0
            if auto:
                self.auto_flush_count += 1
        # Atomic-write: write to .tmp then os.replace.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps({"keys": snapshot}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)
