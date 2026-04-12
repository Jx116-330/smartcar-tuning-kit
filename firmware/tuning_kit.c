/*
 * tuning_kit.c
 *
 * SmartCar Tuning Kit - Core framework implementation.
 * Transport abstraction, line-based receive buffering,
 * command dispatch, telemetry send loop, and response formatting.
 */

#include "tuning_kit.h"

#include <string.h>
#include <stdio.h>
#include <stdarg.h>

/* ===================== Internal state ===================== */

static const tk_hal_t          *s_hal      = NULL;
static const tk_pid_callbacks_t *s_pid_cbs = NULL;

static volatile uint8_t s_enabled       = 0U;
static uint16_t         s_period_ms     = TK_DEFAULT_PERIOD_MS;
static uint32_t         s_last_send_ms  = 0U;
static uint32_t         s_total_sent    = 0U;
static uint32_t         s_total_fail    = 0U;

/* ===================== Telemetry registry ===================== */

typedef struct
{
    const char       *name;
    tk_tel_builder_t  builder;
} tk_tel_entry_t;

static tk_tel_entry_t s_tel_registry[TK_MAX_TEL_CHANNELS];
static uint8_t        s_tel_count = 0U;

/* ===================== Command registry ===================== */

typedef struct
{
    const char       *prefix;
    uint32_t          prefix_len;
    tk_cmd_handler_t  handler;
} tk_cmd_entry_t;

static tk_cmd_entry_t s_cmd_registry[TK_MAX_CMD_HANDLERS];
static uint8_t        s_cmd_count = 0U;

/* ===================== Registration ===================== */

uint8_t tk_register_telemetry(const char *name, tk_tel_builder_t builder)
{
    if ((NULL == builder) || (s_tel_count >= TK_MAX_TEL_CHANNELS))
    {
        return 0U;
    }
    s_tel_registry[s_tel_count].name    = name;
    s_tel_registry[s_tel_count].builder = builder;
    s_tel_count++;
    return 1U;
}

uint8_t tk_register_command(const char *prefix, tk_cmd_handler_t handler)
{
    if ((NULL == prefix) || (NULL == handler) || (s_cmd_count >= TK_MAX_CMD_HANDLERS))
    {
        return 0U;
    }
    s_cmd_registry[s_cmd_count].prefix     = prefix;
    s_cmd_registry[s_cmd_count].prefix_len = (uint32_t)strlen(prefix);
    s_cmd_registry[s_cmd_count].handler    = handler;
    s_cmd_count++;
    return 1U;
}

/* ===================== Transport helpers ===================== */

static uint8_t tk_send_line(const char *line)
{
    uint32_t remain;
    if ((NULL == line) || (NULL == s_hal) || (NULL == s_hal->transport_send))
    {
        return 0U;
    }

    remain = s_hal->transport_send((const uint8_t *)line, (uint32_t)strlen(line));
    if (0U == remain)
    {
        s_total_sent++;
        return 1U;
    }
    s_total_fail++;
    return 0U;
}

void tk_send_response(const char *line)
{
    char buf[TK_RESP_BUFFER_LEN];

    if ((NULL == line) || (NULL == s_hal))
    {
        return;
    }
    if (s_hal->is_connected && !s_hal->is_connected())
    {
        return;
    }

    snprintf(buf, sizeof(buf), "%s\r\n", line);
    if (s_hal->transport_send)
    {
        if (0U != s_hal->transport_send((const uint8_t *)buf, (uint32_t)strlen(buf)))
        {
            s_total_fail++;
        }
    }
}

void tk_reply_ack(const char *cmd, const char *extra_fmt, ...)
{
    char response[TK_RESP_BUFFER_LEN];
    int  pos;

    pos = snprintf(response, sizeof(response), "ACK,cmd=%s",
                   (NULL != cmd) ? cmd : "UNKNOWN");

    if ((NULL != extra_fmt) && (extra_fmt[0] != '\0') && (pos > 0) && ((uint32_t)pos < sizeof(response) - 3U))
    {
        va_list ap;
        va_start(ap, extra_fmt);
        pos += vsnprintf(response + pos, sizeof(response) - (uint32_t)pos - 3U, extra_fmt, ap);
        va_end(ap);
    }

    /* Append \r\n */
    if ((pos > 0) && ((uint32_t)pos < sizeof(response) - 2U))
    {
        response[pos++] = '\r';
        response[pos++] = '\n';
        response[pos]   = '\0';
    }

    if ((NULL != s_hal) && (NULL != s_hal->transport_send))
    {
        if (0U != s_hal->transport_send((const uint8_t *)response, (uint32_t)strlen(response)))
        {
            s_total_fail++;
        }
    }
}

void tk_reply_error(const char *cmd, const char *reason)
{
    char response[TK_RESP_BUFFER_LEN];

    snprintf(response, sizeof(response), "ERR,cmd=%s,reason=%s\r\n",
             (NULL != cmd) ? cmd : "UNKNOWN",
             (NULL != reason) ? reason : "UNKNOWN");

    if ((NULL != s_hal) && (NULL != s_hal->transport_send))
    {
        if (0U != s_hal->transport_send((const uint8_t *)response, (uint32_t)strlen(response)))
        {
            s_total_fail++;
        }
    }
}

/* ===================== Telemetry sending ===================== */

static void tk_send_telemetry(void)
{
    char line[TK_SEND_BUFFER_LEN];
    uint8_t i;

    if ((NULL == s_hal) || !s_hal->is_connected || !s_hal->is_connected())
    {
        return;
    }

    for (i = 0U; i < s_tel_count; i++)
    {
        if (NULL == s_tel_registry[i].builder)
        {
            continue;
        }

        line[0] = '\0';
        if (s_tel_registry[i].builder(line, sizeof(line) - 3U))
        {
            /* Append \r\n if the builder didn't already */
            uint32_t len = (uint32_t)strlen(line);
            if ((len > 0U) && (line[len - 1U] != '\n'))
            {
                if (len < sizeof(line) - 2U)
                {
                    line[len++] = '\r';
                    line[len++] = '\n';
                    line[len]   = '\0';
                }
            }
            tk_send_line(line);
        }
    }
}

/* ===================== Command dispatch ===================== */

static void tk_trim_line(char *line)
{
    char *end;
    /* Trim leading spaces */
    while ((*line == ' ') || (*line == '\t'))
    {
        /* shift left in-place */
        char *src = line + 1;
        char *dst = line;
        while (*src) { *dst++ = *src++; }
        *dst = '\0';
    }
    /* Trim trailing spaces */
    end = line + strlen(line) - 1;
    while ((end >= line) && ((*end == ' ') || (*end == '\t') || (*end == '\r') || (*end == '\n')))
    {
        *end-- = '\0';
    }
}

/* Built-in framework commands */
static uint8_t tk_builtin_handler(const char *line, char *resp, uint32_t resp_len)
{
    if (0 == strcmp(line, "SET TELEMETRY ON"))
    {
        s_enabled = 1U;
        tk_reply_ack("SET_TELEMETRY", ",val=ON");
        return 1U;
    }
    if (0 == strcmp(line, "SET TELEMETRY OFF"))
    {
        s_enabled = 0U;
        tk_reply_ack("SET_TELEMETRY", ",val=OFF");
        return 1U;
    }
    if (0 == strcmp(line, "GET STATUS"))
    {
        snprintf(resp, resp_len,
                 "ACK,cmd=GET_STATUS,enabled=%u,period=%u,sent=%lu,fail=%lu",
                 (unsigned int)s_enabled,
                 (unsigned int)s_period_ms,
                 (unsigned long)s_total_sent,
                 (unsigned long)s_total_fail);
        return 1U;
    }

    /* SET PERIOD <ms> */
    {
        unsigned int val = 0U;
        if (1 == sscanf(line, "SET PERIOD %u", &val))
        {
            tk_set_period((uint16_t)val);
            snprintf(resp, resp_len, "ACK,cmd=SET_PERIOD,val=%u", (unsigned int)s_period_ms);
            return 1U;
        }
    }

    return 0U; /* Not handled */
}

static void tk_dispatch_command(char *line)
{
    char resp[TK_RESP_BUFFER_LEN];
    uint8_t i;

    tk_trim_line(line);
    if (line[0] == '\0')
    {
        return;
    }

    resp[0] = '\0';

    /* Try registered handlers first (in order) */
    for (i = 0U; i < s_cmd_count; i++)
    {
        if (0 == strncmp(line, s_cmd_registry[i].prefix, s_cmd_registry[i].prefix_len))
        {
            if (s_cmd_registry[i].handler(line, resp, sizeof(resp)))
            {
                if (resp[0] != '\0')
                {
                    tk_send_response(resp);
                }
                return;
            }
        }
    }

    /* Try built-in framework commands */
    if (tk_builtin_handler(line, resp, sizeof(resp)))
    {
        if (resp[0] != '\0')
        {
            tk_send_response(resp);
        }
        return;
    }

    /* Unknown command */
    tk_reply_error(line, "UNKNOWN_COMMAND");
}

/* ===================== Receive task ===================== */

static void tk_receive_task(void)
{
    uint8_t  raw[TK_RECV_BUFFER_LEN];
    uint32_t raw_len = 0U;
    static char    line_buf[TK_RECV_BUFFER_LEN];
    static uint32_t line_len = 0U;
    uint32_t i;

    if ((NULL == s_hal) || (NULL == s_hal->transport_recv))
    {
        return;
    }
    if (s_hal->is_connected && !s_hal->is_connected())
    {
        line_len = 0U;
        return;
    }

    raw_len = s_hal->transport_recv(raw, sizeof(raw));
    if (0U == raw_len)
    {
        return;
    }

    for (i = 0U; i < raw_len; i++)
    {
        char ch = (char)raw[i];

        if ('\r' == ch)
        {
            continue;
        }

        if ('\n' == ch)
        {
            line_buf[line_len] = '\0';
            if (line_len > 0U)
            {
                tk_dispatch_command(line_buf);
            }
            line_len = 0U;
            continue;
        }

        if (line_len < (sizeof(line_buf) - 1U))
        {
            line_buf[line_len++] = ch;
        }
        else
        {
            line_len = 0U;
            tk_reply_error("UNKNOWN", "LINE_TOO_LONG");
        }
    }
}

/* ===================== Public API ===================== */

void tk_init(const tk_hal_t *hal, const tk_pid_callbacks_t *pid_cbs)
{
    s_hal          = hal;
    s_pid_cbs      = pid_cbs;
    s_enabled      = 0U;
    s_period_ms    = TK_DEFAULT_PERIOD_MS;
    s_last_send_ms = (hal && hal->get_tick_ms) ? hal->get_tick_ms() : 0U;
    s_total_sent   = 0U;
    s_total_fail   = 0U;
    s_tel_count    = 0U;
    s_cmd_count    = 0U;

    /* Register built-in PID commands */
    if (NULL != pid_cbs)
    {
        tk_pid_register_commands();
    }

    /* Register built-in autotune commands */
    tk_autotune_init();
    tk_autotune_register_commands();
}

void tk_task(void)
{
    uint32_t now_ms;

    /* Always process incoming commands */
    tk_receive_task();

    if (!s_enabled)
    {
        return;
    }

    if ((NULL == s_hal) || (NULL == s_hal->get_tick_ms))
    {
        return;
    }

    now_ms = s_hal->get_tick_ms();
    if ((uint32_t)(now_ms - s_last_send_ms) < (uint32_t)s_period_ms)
    {
        return;
    }

    s_last_send_ms = now_ms;
    tk_send_telemetry();
}

void tk_set_enabled(uint8_t enable)
{
    s_enabled = enable ? 1U : 0U;
}

uint8_t tk_is_enabled(void)
{
    return s_enabled;
}

void tk_set_period(uint16_t period_ms)
{
    if (period_ms < TK_MIN_PERIOD_MS)
    {
        period_ms = TK_MIN_PERIOD_MS;
    }
    if (period_ms > TK_MAX_PERIOD_MS)
    {
        period_ms = TK_MAX_PERIOD_MS;
    }
    s_period_ms = period_ms;
}

uint16_t tk_get_period(void)
{
    return s_period_ms;
}

uint32_t tk_get_total_sent(void)
{
    return s_total_sent;
}

uint32_t tk_get_total_fail(void)
{
    return s_total_fail;
}

/* ===================== Internal accessors for sub-modules ===================== */

const tk_hal_t *tk_get_hal(void)
{
    return s_hal;
}

const tk_pid_callbacks_t *tk_get_pid_callbacks(void)
{
    return s_pid_cbs;
}
