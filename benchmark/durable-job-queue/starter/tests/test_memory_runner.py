from jobboard import JobRunner, JobState, MemoryJobStore


def test_successful_job():
    store = MemoryJobStore()
    job = store.enqueue("double", {"value": 4})
    result = JobRunner(store, {"double": lambda payload: payload["value"] * 2}).run_next()
    assert result is job
    assert job.state == JobState.SUCCEEDED
    assert job.result == 8
    assert job.attempts == 1


def test_failed_job():
    def fail(_payload):
        raise RuntimeError("boom")

    store = MemoryJobStore()
    job = store.enqueue("fail", {})
    JobRunner(store, {"fail": fail}).run_next()
    assert job.state == JobState.FAILED
    assert job.error == "boom"


def test_empty_queue():
    assert JobRunner(MemoryJobStore(), {}).run_next() is None
