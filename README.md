# SmartCar Tuning Kit

An open-source real-time tuning toolkit for smart car projects. Includes a portable C library for the car MCU and a Python desktop GUI for monitoring and parameter tuning.

## Features

- **Real-time telemetry**: stream sensor data from car to desktop via TCP
- **PID tuning**: get/set/save PID parameters, 7-candidate auto-tuning
- **Live plotting**: rolling chart with configurable channels
- **HTTP API**: automate tuning workflows via REST endpoints
- **Config-driven**: customize fields, commands, and UI by editing one Python file
- **Zero dependencies**: desktop tool uses only Python stdlib (tkinter)
- **Portable C library**: callback-based HAL, works on any MCU (TC387, STM32, ESP32, etc.)

## Quick Start

### Desktop Tool

```bash
cd desktop
python tuning_tool.py
```

1. Click **Start** to begin listening for TCP connections
2. Use **Start Sim** to see simulated data without hardware
3. Edit `tuning_config.py` to customize for your project

### Firmware Integration

1. Copy the `firmware/` directory into your project
2. Edit `tuning_kit_config.h` for buffer sizes if needed
3. Implement HAL callbacks:

```c
#include "tuning_kit.h"

// Implement these for your platform:
static uint32_t my_send(const uint8_t *data, uint32_t len) { /* ... */ }
static uint32_t my_recv(uint8_t *buf, uint32_t max_len)    { /* ... */ }
static uint8_t  my_is_connected(void)                       { /* ... */ }
static uint32_t my_tick_ms(void)                            { /* ... */ }

static const tk_pid_param_t* my_get_pid(void)               { /* ... */ }
static uint8_t my_set_pid(const tk_pid_param_t *p, uint8_t save) { /* ... */ }

// Register a telemetry packet:
static uint8_t my_telemetry_builder(char *buf, uint32_t len) {
    snprintf(buf, len, "TELG,ms=%lu,gx=%.3f,gy=%.3f,gz=%.3f",
             my_tick_ms(), gyro_x, gyro_y, gyro_z);
    return 1;
}

void main_init(void) {
    tk_hal_t hal = { my_send, my_recv, my_is_connected, my_tick_ms };
    tk_pid_callbacks_t pid = { my_get_pid, my_set_pid };
    tk_init(&hal, &pid);
    tk_register_telemetry("TELG", my_telemetry_builder);
    tk_set_enabled(1);
}

void main_loop(void) {
    tk_task();  // Call every 1-10 ms
}
```

## Project Structure

```
smartcar-tuning-kit/
├── firmware/                    # Car-side C library
│   ├── tuning_kit.h/c          # Core framework
│   ├── tuning_pid.h/c          # PID types + command handlers
│   ├── tuning_autotune.h/c     # Auto-tuning algorithm
│   ├── tuning_kit_config.h     # Compile-time config
│   └── example_yaw/            # Yaw tuning example
│       ├── tuning_yaw.h/c      # TELY packet + YAW commands
│
├── desktop/                     # Desktop Python tool
│   ├── tuning_tool.py           # Main GUI (framework, don't edit)
│   ├── tuning_config.py         # Your configuration (edit this)
│   └── tuning_config_yaw.py     # Yaw example config
│
├── protocol.md                  # Wire protocol specification
└── README.md                    # This file
```

## Configuration

All customization is done in `desktop/tuning_config.py`:

| Setting | Purpose |
|---------|---------|
| `PLOT_KEYS` | Chart channels: `[(key, color, default_on), ...]` |
| `PRIMARY_METRICS` | Big metric cards: `[(key, label), ...]` |
| `DETAIL_METRICS` | Compact metric rows |
| `EXTENDED_METRICS` | Scrollable metric area |
| `QUICK_COMMANDS` | Sidebar command buttons |
| `KEY_MAP` | Short key expansion: `{short: long, ...}` |
| `CUSTOM_TABS` | Extra status tabs with live fields |
| `COMMAND_TABS` | Command tabs with buttons + parameter inputs |
| `TELEMETRY_PREFIXES` | Recognized telemetry packet types |
| `RESPONSE_PREFIXES` | Recognized response packet types |

### Yaw Tuning Example

To enable yaw tuning UI, replace `tuning_config.py` with `tuning_config_yaw.py`:

```bash
cd desktop
copy tuning_config_yaw.py tuning_config.py
python tuning_tool.py
```

## HTTP API

The desktop tool exposes a REST API at `http://127.0.0.1:9898`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/latest` | GET | Latest telemetry + custom state |
| `/snapshot` | GET | Full snapshot with connection info |
| `/history` | GET | Recent telemetry history (100 entries) |
| `/status` | GET | Server status |
| `/command` | POST | Send command to car: `{"command": "GET PID"}` |

## Protocol

See [protocol.md](protocol.md) for the complete wire protocol specification.

## License

MIT
