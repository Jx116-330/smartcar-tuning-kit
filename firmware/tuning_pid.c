/*
 * tuning_pid.c
 *
 * SmartCar Tuning Kit - Built-in PID command handlers.
 * Handles: GET PID, SET PID, SET KP/KI/KD, SAVE PID.
 */

#include "tuning_kit.h"

#include <string.h>
#include <stdio.h>

/* ===================== Internal helpers ===================== */

/* Forward declaration of accessors from tuning_kit.c */
extern const tk_hal_t          *tk_get_hal(void);
extern const tk_pid_callbacks_t *tk_get_pid_callbacks(void);

static void tk_pid_reply_ack(const char *cmd)
{
    const tk_pid_callbacks_t *cbs = tk_get_pid_callbacks();
    const tk_pid_param_t *p = NULL;
    const tk_hal_t *hal = tk_get_hal();
    uint32_t ms = 0U;

    if ((NULL != cbs) && (NULL != cbs->get_param))
    {
        p = cbs->get_param();
    }
    if ((NULL != hal) && (NULL != hal->get_tick_ms))
    {
        ms = hal->get_tick_ms();
    }

    tk_reply_ack(cmd, ",ms=%lu,kp=%.3f,ki=%.4f,kd=%.3f",
                 (unsigned long)ms,
                 (NULL != p) ? p->kp : 0.0f,
                 (NULL != p) ? p->ki : 0.0f,
                 (NULL != p) ? p->kd : 0.0f);
}

/* ===================== Command handler ===================== */

static uint8_t tk_pid_cmd_handler(const char *line, char *resp, uint32_t resp_len)
{
    const tk_pid_callbacks_t *cbs = tk_get_pid_callbacks();
    (void)resp;
    (void)resp_len;

    if (NULL == cbs)
    {
        return 0U;
    }

    /* --- GET PID / GET PID RUNTIME --- */
    if ((0 == strcmp(line, "GET PID")) || (0 == strcmp(line, "GET PID RUNTIME")))
    {
        tk_pid_reply_ack("GET_PID_RUNTIME");
        return 1U;
    }

    /* --- SET PID <kp> <ki> <kd> --- */
    {
        tk_pid_param_t param;
        if (3 == sscanf(line, "SET PID %f %f %f", &param.kp, &param.ki, &param.kd))
        {
            if ((NULL != cbs->get_param) && (NULL != cbs->set_param))
            {
                const tk_pid_param_t *cur = cbs->get_param();
                if (NULL != cur)
                {
                    param.integral_limit = cur->integral_limit;
                    param.output_limit   = cur->output_limit;
                }
                else
                {
                    param.integral_limit = 0.0f;
                    param.output_limit   = 0.0f;
                }

                if (cbs->set_param(&param, 0U))
                {
                    tk_pid_reply_ack("SET_PID");
                }
                else
                {
                    tk_reply_error("SET_PID", "OUT_OF_RANGE");
                }
            }
            return 1U;
        }
    }

    /* --- SET KP/KI/KD <value> --- */
    {
        char cmd_name[16];
        float value = 0.0f;
        if (2 == sscanf(line, "SET %15s %f", cmd_name, &value))
        {
            if ((NULL == cbs->get_param) || (NULL == cbs->set_param))
            {
                return 0U;
            }

            const tk_pid_param_t *cur = cbs->get_param();
            if (NULL == cur)
            {
                return 0U;
            }

            tk_pid_param_t param = *cur;
            uint8_t matched = 0U;

            if (0 == strcmp(cmd_name, "KP"))       { param.kp = value; matched = 1U; }
            else if (0 == strcmp(cmd_name, "KI"))   { param.ki = value; matched = 1U; }
            else if (0 == strcmp(cmd_name, "KD"))   { param.kd = value; matched = 1U; }

            if (!matched)
            {
                return 0U; /* Not a PID command, let other handlers try */
            }

            if (cbs->set_param(&param, 0U))
            {
                tk_pid_reply_ack(cmd_name);
            }
            else
            {
                tk_reply_error(cmd_name, "OUT_OF_RANGE");
            }
            return 1U;
        }
    }

    /* --- SAVE PID --- */
    if (0 == strcmp(line, "SAVE PID"))
    {
        if ((NULL != cbs->get_param) && (NULL != cbs->set_param))
        {
            const tk_pid_param_t *cur = cbs->get_param();
            if ((NULL != cur) && cbs->set_param(cur, 1U))
            {
                tk_pid_reply_ack("SAVE_PID");
            }
            else
            {
                tk_reply_error("SAVE_PID", "FLASH_SAVE_FAILED");
            }
        }
        return 1U;
    }

    return 0U; /* Not handled */
}

/* ===================== Registration ===================== */

void tk_pid_register_commands(void)
{
    tk_register_command("GET PID", tk_pid_cmd_handler);
    tk_register_command("SET PID", tk_pid_cmd_handler);
    tk_register_command("SET KP",  tk_pid_cmd_handler);
    tk_register_command("SET KI",  tk_pid_cmd_handler);
    tk_register_command("SET KD",  tk_pid_cmd_handler);
    tk_register_command("SAVE PID", tk_pid_cmd_handler);
}
