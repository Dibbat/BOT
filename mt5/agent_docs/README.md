# MT5 Agent Documentation Pack

This folder is a code-accurate operating manual for future agents working on the MT5 stack in this repository.

Scope:
- Thinker signal generation
- Bridge execution lifecycle
- Risk and safety guards
- Config semantics and per-symbol overrides
- Log interpretation and validation checklist

Source of truth:
- `pt_mt5_thinker.py`
- `pt_mt5_bridge.py`
- `pt_mt5_trainer.py`
- `mt5_config.json`

Read order for new agents:
1. `01-runtime-architecture.md`
2. `02-thinker-and-signal-generation.md`
3. `03-bridge-order-lifecycle.md`
4. `04-risk-and-safety-controls.md`
5. `05-config-field-reference.md`
6. `06-log-decoding-and-validation-checklist.md`
7. `07-trainer-memory-lifecycle.md`

Important context:
- This project has a legacy `.venv/mt5` mirror in some environments. Use the canonical files in the top-level `mt5` folder as the active implementation unless explicitly told otherwise.
- Current behavior intentionally avoids long/short mirrored scoring by:
  - direction-aware short scoring in thinker
  - mirrored short-memory guard in exporter
  - disabling long-to-short auto-copy in trainer
