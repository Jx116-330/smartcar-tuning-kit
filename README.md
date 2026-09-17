# SmartCar Tuning Kit

An open-source tuning toolkit for smart car (and other embedded) projects: a portable C library for the MCU side, and a Python **TCP↔HTTP bridge + Web console** for the desktop side — monitoring, parameter tuning, automated optimization and safety-guarded experimentation.

> **v2 baseline (2026-09)** — the `desktop/` half has been rebuilt around a generic
> bridge core: schema-driven Web UI, TPE Bayesian optimizer, declarative guardrails,
> MCP tool surface for AI agents, a bridge-extension mechanism for device-specific
> features, and a headless service mode. The legacy `firmware/` library keeps working
> with it via the original `key=value` protocol. 中文详细文档见 [`desktop/README.md`](desktop/README.md)
> 与 [`desktop/docs/`](desktop/docs/)。

## What's inside

```
desktop/     Python bridge (TCP 8080 ↔ HTTP 9898) + web console + tooling
firmware/    Portable C library for the car MCU (legacy key=value protocol)
protocol.md  Wire protocol reference for the legacy firmware library
```

## Desktop tool highlights

- **Generic bridge core** — config-driven, zero device-specific strings in the core; per-device profiles (`desktop/profiles/`) swap protocol, schema and guardrails
- **Web console** — Vite + React + uPlot, schema-generated tuning panel, live SSE telemetry, trajectory view, run comparison (`/ui`)
- **Smart optimizer** — budget-aware TPE Bayesian optimization with multi-objective Pareto layers, deterministic seeds (`desktop/optimizer.py`)
- **Safety guardrails** — blacklists / value ranges / max-step / per-run rollback declared in schema, enforced at bridge, driver and MCP layers with append-only audit (`desktop/guardrails.py`)
- **Scoring & attribution** — configurable score profiles with per-segment breakdown ("where points were lost")
- **Agent surface** — stdio MCP server (`desktop/mcp_server.py`) with propose→human-confirm flow
- **Bridge extensions** — device-specific endpoints/stream parsing live outside the core in a config-declared extension module (`desktop/bridge_ext.py` as the reference implementation)
- **Headless mode** — `python tuning_tool.py --headless`: no GUI required, file logging, `POST /shutdown` graceful exit; console exe build for service deployments

## Quick start

### Desktop tool (zero hardware)

```bash
cd desktop

# 1. fake firmware device (virtual profile demo chain)
python profiles/virtual/virtual_device.py

# 2. in another terminal, start the bridge with the virtual profile
python tuning_tool.py --profile virtual        # GUI mode
python tuning_tool.py --profile virtual --headless   # service mode

# 3. open the web console
#    http://127.0.0.1:9898/ui
```

Web console build (optional, only needed for `npm run build`):

```bash
cd web && npm install && npm run build
```

### Tests

```bash
python tests/run_p0_checks.py               # 10 suites (frozen-exe suite SKIPs without built artifacts)
python tests/test_frozen_headless.py --require   # release gate after build.bat
```

### Firmware integration

- **New devices** — see [`desktop/docs/protocol_contract_v1.md`](desktop/docs/protocol_contract_v1.md)
  and the single-file C99 reference implementation in [`desktop/firmware_kit/`](desktop/firmware_kit/) (line protocol, channel scheduling, `SET/RATE/GET/PING`, `!<seq>` receipts)
- **Legacy MCU library** — copy `firmware/` into your project, implement the HAL
  callbacks (`send`/`recv`/`is_connected`), see `protocol.md`

### Packaging (Windows)

```bat
cd desktop && build.bat
:: -> dist/YawTuningTool.exe (windowed)
:: -> dist/YawTuningToolConsole.exe (console, recommended for headless/service)
```

## License

See [LICENSE](LICENSE).
