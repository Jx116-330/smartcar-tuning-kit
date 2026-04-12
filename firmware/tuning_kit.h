/*
 * tuning_kit.h
 *
 * SmartCar Tuning Kit - Public API header.
 * This is the ONLY header the user needs to include.
 *
 * Usage:
 *   1. Implement tk_hal_t callbacks for your platform.
 *   2. Implement tk_pid_callbacks_t for your PID storage.
 *   3. Call tk_init() once at startup.
 *   4. Call tk_task() periodically from your main loop.
 *   5. Optionally register custom telemetry and commands.
 */

#ifndef TUNING_KIT_H_
#define TUNING_KIT_H_

#include <stdint.h>
#include <stddef.h>
#include "tuning_kit_config.h"
#include "tuning_pid.h"

/* ===================== HAL abstraction ===================== */

typedef struct
{
    /* Send raw bytes over transport (TCP/UART/etc).
     * Return number of bytes NOT sent (0 = all sent successfully). */
    uint32_t (*transport_send)(const uint8_t *data, uint32_t len);

    /* Read available bytes from transport into buf.
     * Return number of bytes actually read. */
    uint32_t (*transport_recv)(uint8_t *buf, uint32_t max_len);

    /* Return 1 if transport is connected, 0 if not. */
    uint8_t (*is_connected)(void);

    /* Return current system tick in milliseconds. */
    uint32_t (*get_tick_ms)(void);
} tk_hal_t;

/* ===================== Telemetry registration ===================== */

/*
 * Telemetry builder callback: fill buf with a complete line (without \r\n).
 * Return 1 if line should be sent, 0 to skip this cycle.
 * Example: snprintf(buf, buf_len, "TELG,ms=%lu,gx=%.3f", ms, gx);
 */
typedef uint8_t (*tk_tel_builder_t)(char *buf, uint32_t buf_len);

/* Register a telemetry channel. Returns 1 on success, 0 if registry full.
 * The 'name' string is for identification only (not sent on wire). */
uint8_t tk_register_telemetry(const char *name, tk_tel_builder_t builder);

/* ===================== Command registration ===================== */

/*
 * Command handler callback: process a received command line.
 * - full_line: the raw command text (already trimmed).
 * - resp_buf / resp_len: buffer to write the response line (without \r\n).
 * Return 1 if this handler handled the command, 0 if not (try next handler).
 * Use tk_send_response() for multi-line responses or if you prefer not to use resp_buf.
 */
typedef uint8_t (*tk_cmd_handler_t)(const char *full_line, char *resp_buf, uint32_t resp_len);

/* Register a command handler with a prefix string.
 * The dispatcher tries handlers in registration order, using strncmp prefix match.
 * Returns 1 on success, 0 if registry full. */
uint8_t tk_register_command(const char *prefix, tk_cmd_handler_t handler);

/* ===================== Core lifecycle ===================== */

/* Initialize the tuning kit. Call once at startup.
 * hal: platform callbacks (required).
 * pid_cbs: PID parameter callbacks (optional, pass NULL to disable PID commands). */
void tk_init(const tk_hal_t *hal, const tk_pid_callbacks_t *pid_cbs);

/* Main task: call periodically from your main loop (typically every 1-10 ms).
 * Handles telemetry sending (at configured period) and command receiving. */
void tk_task(void);

/* Enable or disable telemetry output. */
void tk_set_enabled(uint8_t enable);
uint8_t tk_is_enabled(void);

/* Set / get telemetry send period in ms. */
void tk_set_period(uint16_t period_ms);
uint16_t tk_get_period(void);

/* ===================== Response helpers ===================== */

/* Send a pre-formatted response line. Appends \r\n automatically.
 * Can be called from within a command handler for multi-line responses. */
void tk_send_response(const char *line);

/* Send a formatted ACK response: "ACK,cmd=<cmd>,<extra_fmt>\r\n" */
void tk_reply_ack(const char *cmd, const char *extra_fmt, ...);

/* Send an error response: "ERR,cmd=<cmd>,reason=<reason>\r\n" */
void tk_reply_error(const char *cmd, const char *reason);

/* ===================== Statistics ===================== */

uint32_t tk_get_total_sent(void);
uint32_t tk_get_total_fail(void);

/* ===================== Autotune (optional) ===================== */

#include "tuning_autotune.h"

#endif /* TUNING_KIT_H_ */
