/*
 * tuning_yaw.c
 *
 * SmartCar Tuning Kit - Yaw tuning example implementation.
 * Registers TELY telemetry packet and YAW-related commands.
 */

#include "tuning_yaw.h"
#include "../tuning_kit.h"

#include <string.h>
#include <stdio.h>

/* ===================== State ===================== */

static const tk_yaw_callbacks_t *s_yaw = NULL;

/* ===================== TELY telemetry builder ===================== */

static uint8_t tely_builder(char *buf, uint32_t buf_len)
{
    float gz_raw = 0, gz_bias = 0, gz_comp = 0, gz_scaled = 0;
    float yaw_int = 0, yaw_final = 0, yaw_corr = 0, yaw_dt = 0;
    uint8_t  zero_busy = 0, zero_ok = 0;
    uint32_t zero_n = 0;
    float    zero_gbz = 0;
    const tk_hal_t *hal;

    if (NULL == s_yaw) return 0U;

    /* Only send if debug is enabled */
    if (s_yaw->get_debug_enable && !s_yaw->get_debug_enable())
    {
        return 0U;
    }

    hal = tk_get_hal();

    if (s_yaw->get_debug)
    {
        s_yaw->get_debug(&gz_raw, &gz_bias, &gz_comp, &gz_scaled,
                         &yaw_int, &yaw_final, &yaw_corr, &yaw_dt);
    }
    if (s_yaw->get_zero_state)
    {
        s_yaw->get_zero_state(&zero_busy, &zero_ok, &zero_n, &zero_gbz);
    }

    snprintf(buf, buf_len,
             "TELY,ms=%lu,gzr=%.3f,gzb=%.3f,gzc=%.3f,gzs=%.4f,gzd=%.3f,"
             "yi=%.2f,yf=%.2f,yc=%.4f,dt=%lu,"
             "dbg=%u,ver=%lu,"
             "ton=%u,tst=%.1f,tdl=%.1f,"
             "zb=%u,zok=%u,zn=%lu,zgz=%.3f",
             (unsigned long)(hal && hal->get_tick_ms ? hal->get_tick_ms() : 0U),
             gz_raw, gz_bias, gz_comp,
             s_yaw->get_scale ? s_yaw->get_scale() : 1.0f,
             gz_scaled,
             yaw_int, yaw_final, yaw_corr,
             (unsigned long)(uint32_t)(yaw_dt * 1000000.0f),
             (unsigned int)(s_yaw->get_debug_enable ? s_yaw->get_debug_enable() : 0U),
             (unsigned long)(s_yaw->get_param_version ? s_yaw->get_param_version() : 0U),
             (unsigned int)(s_yaw->test_is_on ? s_yaw->test_is_on() : 0U),
             s_yaw->test_get_start ? s_yaw->test_get_start() : 0.0f,
             s_yaw->test_get_last_delta ? s_yaw->test_get_last_delta() : 0.0f,
             (unsigned int)zero_busy,
             (unsigned int)zero_ok,
             (unsigned long)zero_n,
             zero_gbz);

    return 1U;
}

/* ===================== YAW command handlers ===================== */

static uint8_t yaw_cmd_get(const char *line, char *resp, uint32_t resp_len)
{
    float gz_raw = 0, gz_bias = 0, gz_comp = 0, gz_scaled = 0;
    float yaw_int = 0, yaw_final = 0, yaw_corr = 0, yaw_dt = 0;
    uint8_t  zero_busy = 0, zero_ok = 0;
    uint32_t zero_n = 0;
    float    zero_gbz = 0;
    const tk_hal_t *hal;

    if (0 != strcmp(line, "GET YAW")) return 0U;
    if (NULL == s_yaw) { tk_reply_error("GET_YAW", "NOT_INITIALIZED"); return 1U; }

    hal = tk_get_hal();

    if (s_yaw->get_debug)
        s_yaw->get_debug(&gz_raw, &gz_bias, &gz_comp, &gz_scaled, &yaw_int, &yaw_final, &yaw_corr, &yaw_dt);
    if (s_yaw->get_zero_state)
        s_yaw->get_zero_state(&zero_busy, &zero_ok, &zero_n, &zero_gbz);

    snprintf(resp, resp_len,
             "ACK,cmd=GET_YAW,ms=%lu,"
             "yaw_bias=%.4f,yaw_scale=%.4f,yaw_dbg=%u,yaw_param_ver=%lu,"
             "yaw_test_on=%u,yaw_test_start=%.2f,yaw_test_last_delta=%.2f,"
             "yaw_zero_busy=%u,yaw_zero_ok=%u,yaw_zero_n=%lu,yaw_zero_gbz=%.4f,"
             "yaw_final=%.2f",
             (unsigned long)(hal && hal->get_tick_ms ? hal->get_tick_ms() : 0U),
             s_yaw->get_manual_bias ? s_yaw->get_manual_bias() : 0.0f,
             s_yaw->get_scale ? s_yaw->get_scale() : 1.0f,
             (unsigned int)(s_yaw->get_debug_enable ? s_yaw->get_debug_enable() : 0U),
             (unsigned long)(s_yaw->get_param_version ? s_yaw->get_param_version() : 0U),
             (unsigned int)(s_yaw->test_is_on ? s_yaw->test_is_on() : 0U),
             s_yaw->test_get_start ? s_yaw->test_get_start() : 0.0f,
             s_yaw->test_get_last_delta ? s_yaw->test_get_last_delta() : 0.0f,
             (unsigned int)zero_busy,
             (unsigned int)zero_ok,
             (unsigned long)zero_n,
             zero_gbz,
             yaw_final);
    return 1U;
}

static uint8_t yaw_cmd_set(const char *line, char *resp, uint32_t resp_len)
{
    char cmd_name[16];
    float value = 0.0f;

    if (2 != sscanf(line, "SET %15s %f", cmd_name, &value)) return 0U;

    if (0 == strcmp(cmd_name, "YAW_BIAS"))
    {
        if (s_yaw && s_yaw->set_manual_bias)
        {
            s_yaw->set_manual_bias(value);
            snprintf(resp, resp_len, "ACK,cmd=SET_YAW_BIAS,val=%.4f,ver=%lu",
                     value,
                     (unsigned long)(s_yaw->get_param_version ? s_yaw->get_param_version() : 0U));
        }
        return 1U;
    }

    if (0 == strcmp(cmd_name, "YAW_SCALE"))
    {
        if (s_yaw && s_yaw->set_scale)
        {
            s_yaw->set_scale(value);
            snprintf(resp, resp_len, "ACK,cmd=SET_YAW_SCALE,val=%.4f,ver=%lu",
                     value,
                     (unsigned long)(s_yaw->get_param_version ? s_yaw->get_param_version() : 0U));
        }
        return 1U;
    }

    if (0 == strcmp(cmd_name, "YAW_DBG"))
    {
        if (s_yaw && s_yaw->set_debug_enable)
        {
            s_yaw->set_debug_enable((uint8_t)((int)value != 0));
            snprintf(resp, resp_len, "ACK,cmd=SET_YAW_DBG,val=%u,ver=%lu",
                     (unsigned int)((int)value != 0),
                     (unsigned long)(s_yaw->get_param_version ? s_yaw->get_param_version() : 0U));
        }
        return 1U;
    }

    return 0U; /* Not a yaw SET command */
}

static uint8_t yaw_cmd_zero(const char *line, char *resp, uint32_t resp_len)
{
    if (0 == strcmp(line, "YAW ZERO"))
    {
        if (s_yaw && s_yaw->zero_start)
        {
            s_yaw->zero_start(1000U); /* 1000 samples @ 1kHz = ~1s */
            snprintf(resp, resp_len, "ACK,cmd=YAW_ZERO,busy=1,target=1000");
        }
        return 1U;
    }

    if (0 == strcmp(line, "YAW ZERO STATUS"))
    {
        if (s_yaw && s_yaw->get_zero_state)
        {
            uint8_t busy = 0, ok = 0;
            uint32_t n = 0;
            float gbz = 0;
            s_yaw->get_zero_state(&busy, &ok, &n, &gbz);
            snprintf(resp, resp_len, "YAWCAL,busy=%u,ok=%u,n=%lu,gbz=%.4f,ver=%lu",
                     (unsigned int)busy, (unsigned int)ok,
                     (unsigned long)n, gbz,
                     (unsigned long)(s_yaw->get_param_version ? s_yaw->get_param_version() : 0U));
        }
        return 1U;
    }

    return 0U;
}

static uint8_t yaw_cmd_test(const char *line, char *resp, uint32_t resp_len)
{
    if (0 == strcmp(line, "YAW TEST START"))
    {
        if (s_yaw && s_yaw->test_start)
        {
            s_yaw->test_start();
            snprintf(resp, resp_len, "ACK,cmd=YAW_TEST_START,start=%.2f",
                     s_yaw->test_get_start ? s_yaw->test_get_start() : 0.0f);
        }
        return 1U;
    }

    if (0 == strcmp(line, "YAW TEST STOP"))
    {
        if (s_yaw && s_yaw->test_stop)
        {
            float start = 0, end = 0, delta = 0, err90 = 0;
            s_yaw->test_stop(&start, &end, &delta, &err90);
            snprintf(resp, resp_len, "YAWTEST,start=%.2f,end=%.2f,delta=%.2f,err90=%.2f",
                     start, end, delta, err90);
        }
        return 1U;
    }

    return 0U;
}

/* ===================== Init ===================== */

void tk_yaw_init(const tk_yaw_callbacks_t *cbs)
{
    s_yaw = cbs;

    /* Register TELY telemetry channel */
    tk_register_telemetry("TELY", tely_builder);

    /* Register command handlers */
    tk_register_command("GET YAW",       yaw_cmd_get);
    tk_register_command("SET YAW_BIAS",  yaw_cmd_set);
    tk_register_command("SET YAW_SCALE", yaw_cmd_set);
    tk_register_command("SET YAW_DBG",   yaw_cmd_set);
    tk_register_command("YAW ZERO",      yaw_cmd_zero);
    tk_register_command("YAW TEST",      yaw_cmd_test);
}
