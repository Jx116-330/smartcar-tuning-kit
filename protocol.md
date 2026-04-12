# SmartCar Tuning Kit - Wire Protocol Specification

## Overview

Line-based ASCII protocol over TCP. Human-readable, easy to debug with any terminal tool.

## Framing

- Each message is a single line terminated by `\r\n`
- UTF-8 encoding
- Maximum line length: 384 bytes
- Fields separated by commas, key-value pairs use `=`

## Telemetry (Car -> Desktop)

Format: `PREFIX,key1=value1,key2=value2,...\r\n`

The PREFIX identifies the packet type. Built-in prefixes:
- `TELG` - Gyroscope data
- `TELA` - Attitude (Euler + quaternion)
- `TELY` - Yaw tuning debug (example)

Users define their own prefixes and fields. Example:

```
TELG,ms=12345,gx=0.123,gy=-0.456,gz=7.890,gxyz=7.92\r\n
TELA,ms=12345,roll=1.23,pitch=-0.45,yaw=87.6,q0=0.99,q1=0.01,q2=0.00,q3=0.10\r\n
```

### Bandwidth Optimization

Use short keys to save bandwidth (expand on desktop side via KEY_MAP):

```
TELY,ms=1000,gzr=-0.15,gzb=-0.09,yf=89.2\r\n
```

Desktop expands: `gzr` -> `gz_raw`, `yf` -> `yaw_final`, etc.

## Commands (Desktop -> Car)

Plain text commands, one per line. No prefix required.

### Built-in PID Commands

| Command | Description | Response |
|---------|-------------|----------|
| `GET PID` | Query current PID parameters | `ACK,cmd=GET_PID_RUNTIME,ms=...,kp=...,ki=...,kd=...` |
| `SET PID <kp> <ki> <kd>` | Set all three PID gains | `ACK,cmd=SET_PID,...` |
| `SET KP <value>` | Set proportional gain | `ACK,cmd=KP,...` |
| `SET KI <value>` | Set integral gain | `ACK,cmd=KI,...` |
| `SET KD <value>` | Set derivative gain | `ACK,cmd=KD,...` |
| `SAVE PID` | Persist PID to flash | `ACK,cmd=SAVE_PID,...` |

### Built-in Auto-Tuning Commands

| Command | Description |
|---------|-------------|
| `AUTO START` | Begin 7-candidate grid search |
| `AUTO STOP` | Abort auto-tuning |
| `AUTO STATUS` | Query auto-tuning state |
| `AUTO SCORE <value>` | Submit performance score for current candidate |
| `AUTO STEP <kp> <ki> <kd>` | Set candidate step sizes |
| `AUTO APPLY BEST [1]` | Apply best candidate (1 = save to flash) |

### Built-in Framework Commands

| Command | Description |
|---------|-------------|
| `SET TELEMETRY ON` | Enable telemetry output |
| `SET TELEMETRY OFF` | Disable telemetry output |
| `SET PERIOD <ms>` | Set telemetry send interval |
| `GET STATUS` | Query framework status |

### Example: Yaw Tuning Commands

| Command | Response |
|---------|----------|
| `GET YAW` | `ACK,cmd=GET_YAW,...` (full yaw state) |
| `SET YAW_BIAS <dps>` | `ACK,cmd=SET_YAW_BIAS,val=...,ver=...` |
| `SET YAW_SCALE <factor>` | `ACK,cmd=SET_YAW_SCALE,val=...,ver=...` |
| `YAW ZERO` | `ACK,cmd=YAW_ZERO,busy=1,target=1000` |
| `YAW ZERO STATUS` | `YAWCAL,busy=...,ok=...,n=...,gbz=...` |
| `YAW TEST START` | `ACK,cmd=YAW_TEST_START,start=...` |
| `YAW TEST STOP` | `YAWTEST,start=...,end=...,delta=...,err90=...` |

## Responses (Car -> Desktop)

### ACK (Success)

```
ACK,cmd=<COMMAND_NAME>,key1=value1,...\r\n
```

### ERR (Error)

```
ERR,cmd=<COMMAND_NAME>,reason=<REASON_CODE>\r\n
```

Error codes:
- `UNKNOWN_COMMAND` - Command not recognized
- `OUT_OF_RANGE` - Parameter value out of bounds
- `FLASH_SAVE_FAILED` - Flash write error
- `START_FAILED` - Auto-tune start failed
- `STATE_INVALID` - Wrong state for this command
- `BEST_UNAVAILABLE` - No best candidate yet
- `LINE_TOO_LONG` - Input exceeded buffer size

### Custom Response Types

Custom response types (e.g., `YAWCAL`, `YAWTEST`) follow the same `PREFIX,key=value,...` format.

## Example Session

```
Desktop -> Car:   GET PID
Car -> Desktop:   ACK,cmd=GET_PID_RUNTIME,ms=5000,kp=1.200,ki=0.010,kd=0.500

Desktop -> Car:   SET KP 1.5
Car -> Desktop:   ACK,cmd=KP,ms=5100,kp=1.500,ki=0.010,kd=0.500

Desktop -> Car:   SET TELEMETRY ON
Car -> Desktop:   ACK,cmd=SET_TELEMETRY,val=ON
Car -> Desktop:   TELG,ms=5200,gx=0.12,gy=-0.34,gz=5.67,gxyz=5.68,...
Car -> Desktop:   TELG,ms=5300,gx=0.11,gy=-0.33,gz=5.65,gxyz=5.66,...
...

Desktop -> Car:   AUTO START
Car -> Desktop:   ACK,cmd=AUTO_START,state=2,idx=0,total=7,...
```

## Adding Custom Commands

On the car side, register a command handler:

```c
uint8_t my_cmd_handler(const char *line, char *resp, uint32_t resp_len) {
    if (0 == strcmp(line, "GET MOTOR")) {
        snprintf(resp, resp_len, "ACK,cmd=GET_MOTOR,speed=%.1f,pwm=%d", speed, pwm);
        return 1U;
    }
    return 0U; // Not handled
}

// In init:
tk_register_command("GET MOTOR", my_cmd_handler);
```

On the desktop side, add to `tuning_config.py`:

```python
QUICK_COMMANDS = [
    {'label': 'GET MOTOR', 'command': 'GET MOTOR'},
]
```
