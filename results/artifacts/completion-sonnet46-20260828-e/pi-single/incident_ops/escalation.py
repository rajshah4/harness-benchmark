"""Escalation worker for the durable incident store."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sqlite_store import SQLiteIncidentStore

from .models import Incident


class EscalationWorker:
    """Background worker that claims and completes overdue escalations.

    Parameters
    ----------
    store:
        A :class:`SQLiteIncidentStore` instance.
    worker_id:
        A unique identifier for this worker (used for lease ownership).
    """

    def __init__(self, store: "SQLiteIncidentStore", worker_id: str) -> None:
        self._store = store
        self._worker_id = worker_id

    def run_once(self, now: float | None = None) -> Incident | None:
        """Claim one overdue incident, escalate it, and return the snapshot.

        Returns ``None`` if there is nothing due.
        """
        incident = self._store.claim_due_escalation(self._worker_id, now=now)
        if incident is None:
            return None
        return self._store.complete_escalation(incident.id, self._worker_id, now=now)

    def run_until_idle(
        self,
        max_incidents: int | None = None,
        now: float | None = None,
    ) -> list[Incident]:
        """Drain all overdue incidents up to *max_incidents*.

        Returns the list of escalated incident snapshots.
        """
        results: list[Incident] = []
        while True:
            if max_incidents is not None and len(results) >= max_incidents:
                break
            incident = self.run_once(now=now)
            if incident is None:
                break
            results.append(incident)
        return results
