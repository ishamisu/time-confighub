# SPDX-FileCopyrightText: 2026 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause

"""
Persistent TCC state storage helpers.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from time_config_hub.exceptions import TCCConfigError


class TCCStateStore:
    """Persist and retrieve lightweight TCC state metadata."""

    def __init__(self, config_dir: Path):
        self._config_dir = config_dir
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self._config_dir / "tcc_state.json"

    def save_applied_config(self, config_file: str) -> None:
        """Persist metadata and snapshot for an applied TCC configuration."""
        try:
            source = Path(config_file)
            tcc_snapshot_file = self._config_dir / f"tcc_applied{source.suffix}"
            shutil.copy2(source, tcc_snapshot_file)

            tcc_state = {
                "status": "configured",
                "last_applied_config": str(source.resolve()),
                "snapshot_file": str(tcc_snapshot_file),
                "last_applied_utc": datetime.now(timezone.utc).isoformat(),
            }
            self.state_file.write_text(
                json.dumps(tcc_state, indent=2), encoding="utf-8"
            )

        except Exception as exc:
            raise TCCConfigError("Failed to apply TCC configuration") from exc

    def load_status(self) -> Dict[str, Any]:
        """Load current TCC status from state storage."""
        try:
            if not self.state_file.exists():
                return {
                    "status": "not_configured",
                    "message": "No TCC profile has been applied yet",
                    "state_file": str(self.state_file),
                }

            raw_state = self.state_file.read_text(encoding="utf-8")
            state = json.loads(raw_state)
            state["state_file"] = str(self.state_file)
            return state

        except json.JSONDecodeError as exc:
            raise TCCConfigError("Invalid TCC state data") from exc
        except Exception as exc:
            raise TCCConfigError("Failed to get TCC status") from exc

    def reset(self) -> bool:
        """Reset persisted TCC state to default."""
        try:
            if self.state_file.exists():
                self.state_file.unlink()

            for candidate in self._config_dir.glob("tcc_applied.*"):
                candidate.unlink()

            return True

        except Exception as exc:
            raise TCCConfigError("Failed to reset TCC configuration") from exc
