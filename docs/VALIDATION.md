# Validation

Run these from the project root after the Kali image build finishes:

```text
# Windows PowerShell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -q
python scripts\native-docker-smoke.py
slime doctor

# Linux shell
export PYTHONPATH=src
python3 -m unittest discover -s tests -q
python3 scripts/native-docker-smoke.py
slime doctor
```

Expected results:

- Unit tests cover explicit native configuration, endpoint health checks, structured report import, Scheduler validation, and service bindings.
- The container smoke test confirms the Kali image and the installed CLI binaries.
- `doctor` reports `ready.service: true` after configured model Workers pass their endpoint probes.
