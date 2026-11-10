"""Keep field provenance attached to the value actually selected by a merger.

This does not choose metadata, rank sources, or infer missing historical origins.
The caller supplies its selected values, so existing merge policies stay intact.
"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping


def selected_metadata_provenance(
    previous: Mapping, incoming: Mapping, selected: Mapping,
) -> dict:
    old = previous.get("metadata_provenance") or {}
    new = incoming.get("metadata_provenance") or {}
    old = old if isinstance(old, Mapping) else {}
    new = new if isinstance(new, Mapping) else {}
    fields = (previous.keys() | incoming.keys() | selected.keys()) - {"metadata_provenance"}
    # Query-mode and school-holding annotations are not field origins.
    result = {k: deepcopy(v) for k, v in {**old, **new}.items() if k not in fields}
    for field in (old.keys() | new.keys()) & fields:
        value = selected.get(field)
        if value in (None, "", [], {}):
            continue
        origin = None
        if value == previous.get(field):
            origin = old.get(field)
            if not origin and value == incoming.get(field):
                origin = new.get(field)
        elif value == incoming.get(field):
            origin = new.get(field)
        if origin:
            result[field] = deepcopy(origin)
    return result
