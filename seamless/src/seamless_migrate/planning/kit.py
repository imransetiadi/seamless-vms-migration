"""Names the VMware migration kit gives (SDD §7.2): its destination server, the prefix of its
ports and its volumes are all named after the VM by SafeVmName of the pinned kit 2.2.7."""

from __future__ import annotations

import re

#: the migration kit's transliteration table (SafeVmName in plugins/module_utils/utils.go of the
#: pinned 2.2.7), as the kit has it
KIT_TRANSLITERATIONS = {
    **dict(zip("áéíóúÁÉÍÓÚñÑüÜ", "aeiouAEIOUnNuU", strict=True)),
    **dict(zip("ãõàâêôçÃÕÀÂÊÔÇ", "aoaaeocAOAAEOC", strict=True)),
    **dict(zip("èëîïùûÈËÎÏÙÛ", "eeiiuuEEIIUU", strict=True)),
    **{"æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss"},
    **dict(zip("äöÄÖìòÌÒ", "aoAOioIO", strict=True)),
    **dict.fromkeys("·\u2019\u2013\u2014\u00a0", "_"),
}


def kit_vm_name(name: str) -> str:
    """The VM's name as the migration kit writes it into every name it gives - its destination
    server, the prefix of its ports (SDD §7.2): transliterated, every other character outside
    [A-Za-z0-9_-] written ``_``, runs of ``_`` and of ``-`` collapsed, cut to 64 characters, its
    trailing ``_`` and ``-`` removed (SafeVmName of the pinned kit 2.2.7)."""
    safe = "".join(KIT_TRANSLITERATIONS.get(ch, ch) for ch in name)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", safe)
    safe = re.sub(r"-+", "-", re.sub(r"_+", "_", safe))
    return safe[:64].rstrip("_-")
