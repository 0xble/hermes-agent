"""CLI-prepared completion verifier configuration, separate from model-facing arguments."""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CompletionConfig:
    script: Optional[str]
    sha256: Optional[str]

    def fields(self) -> dict[str, Optional[str]]:
        return {"completion_script": self.script, "completion_script_sha256": self.sha256}
