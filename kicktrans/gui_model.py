"""Small, dependency-free models used by the Kick Translator GUI.

The engine emits structured :class:`~kicktrans.events.EngineEvent` objects.  This
module keeps the widgets independent from the engine implementation and makes
message updates idempotent: a later ``translated`` or ``posted`` event updates
the row created by ``received`` instead of adding another row.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class MessageRecord:
    """The fields shown for one normalized chat message."""

    message_id: str
    timestamp: str = ""
    sender: str = ""
    original: str = ""
    translated: str = ""
    source_lang: str = ""
    target_lang: str = ""
    status: str = ""
    detail: str = ""

    @classmethod
    def from_event(cls, event: Any, message_id: str) -> "MessageRecord":
        record = cls(message_id=message_id)
        record.update(event)
        return record

    def update(self, event: Any) -> None:
        """Merge non-empty structured event fields into this record."""

        for field in (
            "timestamp",
            "sender",
            "original",
            "translated",
            "source_lang",
            "target_lang",
            "status",
            "detail",
        ):
            value = getattr(event, field, "")
            if value not in (None, ""):
                setattr(self, field, str(value))

    @property
    def languages(self) -> str:
        if self.source_lang and self.target_lang:
            return f"{self.source_lang} → {self.target_lang}"
        return self.source_lang or self.target_lang


class MessageStore:
    """Bounded insertion-ordered message collection for the message table."""

    def __init__(self, limit: int = 1000) -> None:
        self.limit = max(1, int(limit))
        self.records: "OrderedDict[str, MessageRecord]" = OrderedDict()

    def upsert(self, event: Any) -> tuple[MessageRecord, str | None]:
        """Apply an event and return ``(record, evicted_id)``.

        Engine events normally carry a ``message_id``.  Empty IDs can still be
        displayed, but each such event is kept under a deterministic synthetic
        key supplied by the caller so that unrelated messages are not merged.
        """

        message_id = str(getattr(event, "message_id", "") or "")
        if not message_id:
            raise ValueError("message_id is required for a message row")
        record = self.records.get(message_id)
        if record is None:
            record = MessageRecord.from_event(event, message_id)
            self.records[message_id] = record
        else:
            record.update(event)
            self.records.move_to_end(message_id)

        evicted_id: str | None = None
        while len(self.records) > self.limit:
            evicted_id, _ = self.records.popitem(last=False)
        return record, evicted_id

    def filtered(self, query: str = "", status_filter: str = "") -> Iterable[MessageRecord]:
        """Yield records matching the case-insensitive UI search/filter."""

        needle = (query or "").strip().casefold()
        wanted = (status_filter or "").strip().casefold()
        for record in self.records.values():
            if wanted and wanted not in ("all", "すべて", "全て"):
                if record.status.casefold() != wanted:
                    continue
            if needle:
                haystack = " ".join(
                    (
                        record.sender,
                        record.original,
                        record.translated,
                        record.source_lang,
                        record.target_lang,
                        record.status,
                    )
                ).casefold()
                if needle not in haystack:
                    continue
            yield record
