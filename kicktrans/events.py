"""Structured UI updates. Engine log queues remain backward compatible."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class EngineEvent:
    kind: str
    state: str = ""
    message_id: str = ""
    sender: str = ""
    original: str = ""
    translated: str = ""
    source_lang: str = ""
    target_lang: str = ""
    status: str = ""
    detail: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().astimezone().strftime("%H:%M:%S"))
