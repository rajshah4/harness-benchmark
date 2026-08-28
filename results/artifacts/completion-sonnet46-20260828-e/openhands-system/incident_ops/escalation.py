"""Background escalation worker."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .models import Incident

if TYPE_CHECKING:
    from .sqlite_store import SQLiteIncidentStore


class EscalationWorker:
    """Claims and completes overdue incident escalations."""

    def __init__(
        self,
        store: "SQLiteIncidentStore",
        worker_id: str | None = None,
    ) -> None:
        self._store = store
        self._worker_id = worker_id or str(uuid.uuid4())

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def run_once(self, now: float | None = None) -> Incident | None:
        """Claim and complete one overdue incident. Returns the updated snapshot or None."""
        incident = self._store.claim_due_escalation(self._worker_id, now=now)
        if incident is None:
            return None
        return self._store.complete_escalation(incident.id, self._worker_id, now=now)

    def run_until_idle(
        self,
        max_incidents: int | None = None,
        now: float | None = None,
    ) -> list[Incident]:
        """Process overdue incidents until none remain (or max_incidents reached)."""
        completed: list[Incident] = []
        while max_incidents is None or len(completed) < max_incidents:
            result = self.run_once(now=now)
            if result is None:
                break
            completed.append(result)
        return completed
