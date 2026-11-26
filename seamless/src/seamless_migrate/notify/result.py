"""What a channel's sender answers (SDD §8.7), apart from the notifier that reads it: the senders
import it from here, so none of them imports the notifier, which imports them (no import cycle)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SendResult:
    """What a sender answers (SDD §8.7): final at once, or to be tried again."""

    outcome: Literal["sent", "failed", "retry"]
    reason: str | None = None
    #: a Retry-After the answer carried, in seconds
    retry_after_s: float | None = None
    #: an email's refused recipients
    refused: int = 0
