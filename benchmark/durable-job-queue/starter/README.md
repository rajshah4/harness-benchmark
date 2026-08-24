# Jobboard

Jobboard is a deliberately small background-job package used in the ODSC harness engineering workshop.

The starter implementation runs jobs in memory. It provides a stable regression surface before durable execution is added.

```bash
python -m pytest
PYTHONPATH=src python -m jobboard.cli echo '{"message": "hello"}'
```
