# FrostCore Read-Only Container Deployment Template

This template keeps the runtime code tree mounted `:ro` and isolates all mutable state inside the portable `FrostSeed` volume (`bot.seed/`).

## Preflight Dry-Run Check

```bash
FROSTCORE_DRY_RUN=1 python examples/01_quickstart.py
```

`FROSTCORE_DRY_RUN=1` verifies seed locking, schema initialization, persona integrity, and `/health` probe readiness without connecting external adapters.
