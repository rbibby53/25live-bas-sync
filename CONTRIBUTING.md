# Contributing

Thanks for helping improve 25Live → Niagara Schedule Sync! This started as a
single-campus integration and the goal is to make it work cleanly for any
25Live + Niagara site.

## Ways to help

- **Adapt to other configurations** — different Niagara web services (REST vs
  oBIX vs BACnet), different Series25 instances, other timezones/locales.
- **Bug reports** — include your Python version, OS, a redacted snippet of the
  relevant 25Live XML or Niagara response, and the log output.
- **Docs** — clarify setup for a configuration you got working.

Please **do not** include real credentials, hostnames, or full data dumps in
issues or PRs.

## Development setup

```bash
git clone https://github.com/rbibby53/25Live_Niagara_Sync.git
cd 25Live_Niagara_Sync
python -m venv .venv && . .venv/bin/activate    # (.venv\Scripts\activate on Windows)
pip install -r requirements.txt
cp config.example.yaml config.yaml              # edit for your test instance
cp space_mapping.example.yaml space_mapping.yaml
```

Run the offline tests (no 25Live/Niagara required):

```bash
python Test.py
```

Validate end-to-end safely with a dry run (talks to 25Live, never writes to
Niagara):

```bash
python main.py --dry-run
```

## Guidelines

- **Keep the core generic.** Anything institution-specific belongs in
  `config.yaml`/`space_mapping.yaml`, not in `main.py`.
- **Add a test** for logic changes — `Test.py` covers the pure logic without
  network access; follow that pattern.
- **Flag, don't hardcode, instance-specific assumptions.** Where the 25Live or
  Niagara contract may vary, add a clear comment and a single point of change
  (as done for `NIAGARA_REST_BASE` and the `state` param).
- Match the surrounding style; keep functions small and commented where the
  *why* isn't obvious.

## Pull requests

1. Branch from `main`.
2. Make focused changes; run `python -m py_compile *.py` and `python Test.py`.
3. Describe what changed, why, and anything reviewers should verify against a
   live instance.

By contributing, you agree your contributions are licensed under the project's
GPL-3.0 license.
