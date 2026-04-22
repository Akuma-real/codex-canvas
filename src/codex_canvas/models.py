from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class GenerationPhase(Enum):
    VALIDATING_INPUT = "参数校验"
    RUNNING_CODEX = "调用 codex exec"
    SCANNING_NEW_IMAGE = "扫描新图"
    COPYING_OUTPUT = "复制到输出目录"
    PRESENTING_RESULT = "展示结果"

    @property
    def label(self) -> str:
        return self.value


@dataclass(slots=True, frozen=True)
class ReferenceImage:
    id: str
    path: Path
    created_at: datetime
    source: str = "clipboard"


@dataclass(slots=True, frozen=True)
class GenerationRequest:
    prompt: str
    size: str
    quality: str
    output_dir: Path
    reference_images: tuple[ReferenceImage, ...] = field(default_factory=tuple)
    primary_reference_image_id: str | None = None
    image_action: str = "auto"
    output_format: str = "png"
    background: str = "auto"
    compression: int | None = None


@dataclass(slots=True, frozen=True)
class GenerationResult:
    request: GenerationRequest
    success: bool
    phase: GenerationPhase
    summary: str
    elapsed_seconds: float
    copied_to: Path | None = None
    original_file: Path | None = None
    stdout: str = ""
    stderr: str = ""
    command: tuple[str, ...] = ()
    exit_code: int | None = None
