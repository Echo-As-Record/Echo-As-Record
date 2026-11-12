"""Bounded current forward closure, including fresh edges needed as barriers."""

from collections import deque

from ear_storage_client import Reader


class IncompleteGraph(RuntimeError):
    pass


def closure(reader: Reader, seed: str, limit: int) -> dict[str, set[str]]:
    parents: dict[str, set[str]] = {}
    seen = {seed}
    queue = deque([seed])
    while queue:
        source = queue.popleft()
        for edge in reader.dependents_of(source):
            target = edge.dependent.id
            parents.setdefault(target, set()).add(source)
            if target == seed:
                raise IncompleteGraph("cycle_detected")
            if target not in seen:
                seen.add(target)
                if len(seen) - 1 > limit:
                    raise IncompleteGraph("node_limit")
                queue.append(target)
    # Defensive check: an incomplete/corrupt adapter must not hang the worker.
    remaining = set(parents)
    while remaining:
        ready = {n for n in remaining if not (parents[n] & remaining)}
        if not ready:
            raise IncompleteGraph("cycle_detected")
        remaining -= ready
    return parents
