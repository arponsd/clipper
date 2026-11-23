"""Data shapes shared by the pipeline, CLI and (later) the web UI."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

StageStatus = Literal["pending", "running", "done", "failed", "skipped"]


class Word(BaseModel):
    start: float
    end: float
    text: str


class Segment(BaseModel):
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)


class Transcript(BaseModel):
    language: str
    duration: float
    segments: list[Segment]


class Moment(BaseModel):
    start: float
    end: float
    hook: str = ""
    reason: str = ""
    score: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start


class Keyword(BaseModel):
    """A key word of a clip (G5 keyword emphasis): shown in the emphasis colour in the captions; ``emoji``
    (optional, HyperFrames engine only) is drawn after it."""
    word: str
    emoji: str = ""


class ClipScript(BaseModel):
    """What the LLM writes per clip per language. Editable by the user, then re-rendered."""
    narration: str
    title: str
    description: str
    hashtags: list[str] = Field(default_factory=list)
    # G4: the short hook line shown big over the first seconds ("" = no hook). Scripts from before G4 have none.
    hook: str = ""
    # G5: key words emphasised in the captions (in the clip language, as they appear in the captions).
    keywords: list[Keyword] = Field(default_factory=list)


class StageState(BaseModel):
    status: StageStatus = "pending"
    progress: float = 0.0  # 0..1
    message: str = ""


class ClipLangState(BaseModel):
    stages: dict[str, StageState] = Field(default_factory=dict)
    final: str | None = None  # relative path to final mp4
    error: str | None = None
    # Non-fatal problem of the last render, e.g. the HyperFrames captions fell back to ASS.
    warning: str | None = None


class ClipState(BaseModel):
    index: int
    moment: Moment
    cut: str | None = None
    langs: dict[str, ClipLangState] = Field(default_factory=dict)


class Job(BaseModel):
    id: str
    source: str
    status: Literal["queued", "running", "done", "failed"] = "queued"
    error: str | None = None
    created_at: float
    # Per-job voice-over override (run --no-voiceover / web "options.voiceover"). None = follow the
    # `voiceover` setting.
    voiceover: bool | None = None
    stages: dict[str, StageState] = Field(default_factory=dict)
    clips: list[ClipState] = Field(default_factory=list)
