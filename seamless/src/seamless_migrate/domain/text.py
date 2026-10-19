"""What the store keeps of a string (SDD §11): no NUL and no lone surrogate - PostgreSQL stores
neither, and SQLite cannot encode a lone surrogate."""

from __future__ import annotations

import re

#: a NUL or a lone surrogate - half of a UTF-16 pair, no Unicode text (SDD §11)
UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def replace_unstorable(text: str) -> str:
    """``text`` with each NUL and each lone surrogate written U+FFFD, the replacement character:
    outside text and a source VM's free text the store then keeps wherever it goes (SDD §4.2, §11,
    §13.4)."""
    return UNSTORABLE.sub("\ufffd", text)
