# Contributing

Thanks for helping improve 25Live → BAS Schedule Sync! This started as a
single-campus, single-vendor integration and the goal is to make it work
cleanly for any 25Live site, on any building automation system.

## Ways to help

- **New BAS drivers** — one file implementing `ScheduleWriter`
  (`bassync/drivers/base.py`) plus a line in the registry. Its module docstring
  should document the `target:` syntax operators will type into the room map.
- **Adapt to other configurations** — different Niagara web services, different
  Series25 instances, controllers with unusual `Exception_Schedule` limits,
  other timezones/locales.
- **Bug reports** — include your Python version, OS, which driver you're using,
  a redacted snippet of the relevant 25Live XML or BAS response, and the log
  output from a `--verbose` run.
- **Docs** — clarify setup for a configuration you got working.

Please **do not** include real credentials, hostnames, or full data dumps in
issues or PRs.

## Development setup

```bash
git clone https://github.com/rbibby53/25live-bas-sync.git
cd 25live-bas-sync
python -m venv .venv && . .venv/bin/activate    # (.venv\Scripts\activate on Windows)
pip install -r requirements.txt
pip install -r requirements-bacnet.txt          # only for the bacnet driver
cp config.example.yaml config.yaml              # edit for your test instance
cp space_mapping.example.yaml space_mapping.yaml
```

Run the offline tests (no 25Live and no BAS required):

```bash
python Test.py
```

Validate end-to-end safely (neither mode writes anything):

```bash
python main.py --validate   # config, auth, reachability, schedule targets
python main.py --dry-run    # talks to 25Live only; contacts no BAS at all
```

## Guidelines

- **Keep the core generic.** Anything institution-specific belongs in
  `config.yaml`/`space_mapping.yaml`; anything vendor-specific belongs in a
  driver, not in the shared pipeline.
- **Add a test** for logic changes — `Test.py` covers the pure logic without
  network access; follow that pattern.
- **Flag, don't hardcode, instance-specific assumptions.** Where a 25Live or
  vendor contract may vary, make it a config key with a documented default
  rather than a constant — as done for `rest_base`, `special_event_type` and
  `state_param_style`.
- **Never let a write path fail open.** Anything that could clear a schedule
  when it didn't mean to needs a guard; see `bassync/safety.py` for why.
- Match the surrounding style; keep functions small and commented where the
  *why* isn't obvious.

## Pull requests

1. Branch from `main`.
2. Make focused changes; run `python -m compileall -q main.py editor.py Test.py
   bassync` and `python Test.py`.
3. Describe what changed, why, and anything reviewers should verify against a
   live instance.

By contributing, you agree your contributions are licensed under the project's
GPL-3.0 license.
