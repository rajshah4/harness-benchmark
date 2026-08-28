"""Background escalation worker."""

from __future__ import annotations

from .models import Incident
from .sqlite_store import SQLiteIncidentStore


class EscalationWorker:
    """Claims and completes overdue escalations from the given store.

    A single ``run_once`` call atomically claims one overdue incident for
    *worker_id*, escalates it, and returns the updated snapshot.  Multiple
    workers running concurrently on the same database will never process the
    same incident.
    """

    def __init__(self, store: SQLiteIncidentStore, worker_id: str) -> None:
        self._store = store
        self._worker_id = worker_id

    def run_once(self, now: float | None = None) -> Incident | None:
        """Claim and complete one overdue escalation.

        Returns the updated incident snapshot, or ``None`` when there is
        nothing ready to escalate.
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
        """Repeatedly call :meth:`run_once` until there is nothing left.

        *max_incidents* caps the number of incidents processed in one call.
        Returns every incident snapshot that was escalated.
        """
        results: list[Incident] = []
        while max_incidents is None or len(results) < max_incidents:
            result = self.run_once(now=now)
            if result is None:
                break
            results.append(result)
        return results
