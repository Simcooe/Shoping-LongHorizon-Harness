"""Thread-safe explicit lease tracking for ShopSimulator worker slots."""

from __future__ import annotations

import threading
import uuid


class SlotLeasePool:
    """A slot remains leased until its owner explicitly releases it."""

    def __init__(self, size: int):
        self._lock = threading.Lock()
        self._size = 0
        self._free: set[int] = set()
        self._leases: dict[int, str] = {}
        self.reset(size)

    def reset(self, size: int) -> None:
        size = int(size)
        if size < 0:
            raise ValueError("slot pool size must be non-negative")
        with self._lock:
            self._size = size
            self._free = set(range(size))
            self._leases = {}

    def acquire(self) -> int | None:
        with self._lock:
            if not self._free:
                return None
            slot = self._free.pop()
            self._leases[slot] = uuid.uuid4().hex
            return slot

    def lease_for(self, slot: int) -> str | None:
        with self._lock:
            return self._leases.get(int(slot))

    def verify(self, slot: int, lease_id: str) -> bool:
        with self._lock:
            return self._leases.get(int(slot)) == str(lease_id)

    def release(self, slot: int, lease_id: str | None = None) -> bool:
        slot = int(slot)
        with self._lock:
            if slot < 0 or slot >= self._size:
                raise ValueError(f"invalid environment index: {slot}")
            was_leased = slot not in self._free
            if lease_id is not None and self._leases.get(slot) != str(lease_id):
                return False
            self._leases.pop(slot, None)
            self._free.add(slot)
            return was_leased

    def free_slots(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._free)
