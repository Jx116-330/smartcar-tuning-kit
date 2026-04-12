/*
 * tuning_autotune.c
 *
 * SmartCar Tuning Kit - 7-candidate grid search PID auto-tuning.
 * Adapted from autotune.c.
 */

#include "tuning_autotune.h"
#include "tuning_kit.h"

#include <string.h>
#include <stdio.h>

/* ===================== Constants ===================== */

#define TK_AUTOTUNE_DEFAULT_KP_STEP     0.10f
#define TK_AUTOTUNE_DEFAULT_KI_STEP     0.005f
#define TK_AUTOTUNE_DEFAULT_KD_STEP     0.05f
#define TK_AUTOTUNE_CANDIDATE_TOTAL     7U

typedef struct
{
    float kp_mul;
    float ki_mul;
    float kd_mul;
} tk_autotune_delta_t;

static const tk_autotune_delta_t s_candidates[TK_AUTOTUNE_CANDIDATE_TOTAL] = {
    { 0.0f,  0.0f,  0.0f},   /* Base (no change) */
    { 1.0f,  0.0f,  0.0f},   /* +KP */
    {-1.0f,  0.0f,  0.0f},   /* -KP */
    { 0.0f,  1.0f,  0.0f},   /* +KI */
    { 0.0f, -1.0f,  0.0f},   /* -KI */
    { 0.0f,  0.0f,  1.0f},   /* +KD */
    { 0.0f,  0.0f, -1.0f},   /* -KD */
};

/* ===================== State ===================== */

static tk_autotune_status_t s_status;

/* ===================== Helpers ===================== */

extern const tk_hal_t          *tk_get_hal(void);
extern const tk_pid_callbacks_t *tk_get_pid_callbacks(void);

static uint32_t at_get_tick(void)
{
    const tk_hal_t *hal = tk_get_hal();
    return (hal && hal->get_tick_ms) ? hal->get_tick_ms() : 0U;
}

static float at_clamp(float value, float min_val, float max_val)
{
    if (value < min_val) return min_val;
    if (value > max_val) return max_val;
    return value;
}

static void at_apply_candidate(uint16_t index)
{
    const tk_pid_callbacks_t *cbs = tk_get_pid_callbacks();
    tk_pid_param_t next = s_status.base;

    if (index >= TK_AUTOTUNE_CANDIDATE_TOTAL)
    {
        s_status.state = TK_AUTOTUNE_DONE;
        return;
    }

    next.kp += s_candidates[index].kp_mul * s_status.kp_step;
    next.ki += s_candidates[index].ki_mul * s_status.ki_step;
    next.kd += s_candidates[index].kd_mul * s_status.kd_step;

    next.kp = at_clamp(next.kp, 0.0f, 50.0f);
    next.ki = at_clamp(next.ki, 0.0f, 10.0f);
    next.kd = at_clamp(next.kd, 0.0f, 20.0f);

    if ((NULL == cbs) || (NULL == cbs->set_param) || !cbs->set_param(&next, 0U))
    {
        s_status.state = TK_AUTOTUNE_ERROR;
        return;
    }

    s_status.current         = next;
    s_status.candidate_index = index;
    s_status.updated_ms      = at_get_tick();
    s_status.state           = TK_AUTOTUNE_WAIT_SCORE;
}

/* ===================== Public API ===================== */

void tk_autotune_init(void)
{
    memset(&s_status, 0, sizeof(s_status));
    s_status.kp_step         = TK_AUTOTUNE_DEFAULT_KP_STEP;
    s_status.ki_step         = TK_AUTOTUNE_DEFAULT_KI_STEP;
    s_status.kd_step         = TK_AUTOTUNE_DEFAULT_KD_STEP;
    s_status.candidate_total = TK_AUTOTUNE_CANDIDATE_TOTAL;
    s_status.state           = TK_AUTOTUNE_IDLE;
}

void tk_autotune_reset(void)
{
    const tk_pid_callbacks_t *cbs = tk_get_pid_callbacks();
    tk_pid_param_t current = {0};
    float kp_step = s_status.kp_step;
    float ki_step = s_status.ki_step;
    float kd_step = s_status.kd_step;

    if ((NULL != cbs) && (NULL != cbs->get_param))
    {
        const tk_pid_param_t *p = cbs->get_param();
        if (NULL != p)
        {
            current = *p;
        }
    }

    memset(&s_status, 0, sizeof(s_status));
    s_status.base            = current;
    s_status.current         = current;
    s_status.kp_step         = (kp_step > 0.0f) ? kp_step : TK_AUTOTUNE_DEFAULT_KP_STEP;
    s_status.ki_step         = (ki_step > 0.0f) ? ki_step : TK_AUTOTUNE_DEFAULT_KI_STEP;
    s_status.kd_step         = (kd_step > 0.0f) ? kd_step : TK_AUTOTUNE_DEFAULT_KD_STEP;
    s_status.candidate_total = TK_AUTOTUNE_CANDIDATE_TOTAL;
    s_status.state           = TK_AUTOTUNE_IDLE;
}

uint8_t tk_autotune_start(const tk_pid_param_t *base)
{
    if (NULL == base) return 0U;

    tk_autotune_reset();
    s_status.base       = *base;
    s_status.current    = *base;
    s_status.started_ms = at_get_tick();
    s_status.updated_ms = s_status.started_ms;
    s_status.state      = TK_AUTOTUNE_RUNNING;
    at_apply_candidate(0U);
    return (TK_AUTOTUNE_WAIT_SCORE == s_status.state) ? 1U : 0U;
}

void tk_autotune_stop(void)
{
    if ((TK_AUTOTUNE_IDLE != s_status.state) && (TK_AUTOTUNE_DONE != s_status.state))
    {
        s_status.state      = TK_AUTOTUNE_ABORTED;
        s_status.updated_ms = at_get_tick();
    }
}

uint8_t tk_autotune_is_active(void)
{
    return (TK_AUTOTUNE_RUNNING == s_status.state || TK_AUTOTUNE_WAIT_SCORE == s_status.state) ? 1U : 0U;
}

tk_autotune_state_t tk_autotune_get_state(void)
{
    return s_status.state;
}

const tk_autotune_status_t *tk_autotune_get_status(void)
{
    return &s_status;
}

uint8_t tk_autotune_submit_score(float score)
{
    uint16_t next_index;

    if (TK_AUTOTUNE_WAIT_SCORE != s_status.state) return 0U;

    s_status.last_score = score;
    if ((!s_status.best_valid) || (score > s_status.best_score))
    {
        s_status.best       = s_status.current;
        s_status.best_score = score;
        s_status.best_valid = 1U;
    }

    next_index = (uint16_t)(s_status.candidate_index + 1U);
    if (next_index >= s_status.candidate_total)
    {
        s_status.state      = TK_AUTOTUNE_DONE;
        s_status.updated_ms = at_get_tick();
        return 1U;
    }

    s_status.state = TK_AUTOTUNE_RUNNING;
    at_apply_candidate(next_index);
    return 1U;
}

uint8_t tk_autotune_apply_best(uint8_t save_to_flash)
{
    const tk_pid_callbacks_t *cbs = tk_get_pid_callbacks();
    if (!s_status.best_valid) return 0U;
    if ((NULL == cbs) || (NULL == cbs->set_param)) return 0U;
    return cbs->set_param(&s_status.best, save_to_flash);
}

void tk_autotune_set_steps(float kp_step, float ki_step, float kd_step)
{
    if (kp_step > 0.0f) s_status.kp_step = kp_step;
    if (ki_step > 0.0f) s_status.ki_step = ki_step;
    if (kd_step > 0.0f) s_status.kd_step = kd_step;
}

/* ===================== Command handler ===================== */

static void at_reply_status(const char *cmd)
{
    const tk_autotune_status_t *st = &s_status;
    tk_reply_ack(cmd, ",state=%u,idx=%u,total=%u,best=%u,last=%.3f,bestScore=%.3f,kp=%.3f,ki=%.4f,kd=%.3f",
                 (unsigned int)st->state,
                 (unsigned int)st->candidate_index,
                 (unsigned int)st->candidate_total,
                 (unsigned int)st->best_valid,
                 st->last_score,
                 st->best_score,
                 st->current.kp,
                 st->current.ki,
                 st->current.kd);
}

static uint8_t tk_autotune_cmd_handler(const char *line, char *resp, uint32_t resp_len)
{
    (void)resp;
    (void)resp_len;

    if (0 == strcmp(line, "AUTO STATUS"))
    {
        at_reply_status("AUTO_STATUS");
        return 1U;
    }

    if (0 == strcmp(line, "AUTO STOP"))
    {
        tk_autotune_stop();
        at_reply_status("AUTO_STOP");
        return 1U;
    }

    if (0 == strcmp(line, "AUTO START"))
    {
        const tk_pid_callbacks_t *cbs = tk_get_pid_callbacks();
        if ((NULL != cbs) && (NULL != cbs->get_param))
        {
            const tk_pid_param_t *cur = cbs->get_param();
            if ((NULL != cur) && tk_autotune_start(cur))
            {
                at_reply_status("AUTO_START");
            }
            else
            {
                tk_reply_error("AUTO_START", "START_FAILED");
            }
        }
        else
        {
            tk_reply_error("AUTO_START", "NO_PID_CALLBACKS");
        }
        return 1U;
    }

    /* AUTO APPLY BEST [save_flag] */
    if (0 == strncmp(line, "AUTO APPLY BEST", 15))
    {
        uint8_t save = 0U;
        int val = 0;
        if (1 == sscanf(line + 15, " %d", &val))
        {
            save = (val != 0) ? 1U : 0U;
        }

        if (tk_autotune_apply_best(save))
        {
            at_reply_status("AUTO_APPLY_BEST");
        }
        else
        {
            tk_reply_error("AUTO_APPLY_BEST", "BEST_UNAVAILABLE");
        }
        return 1U;
    }

    /* AUTO STEP <kp_step> <ki_step> <kd_step> */
    {
        float kps, kis, kds;
        if (3 == sscanf(line, "AUTO STEP %f %f %f", &kps, &kis, &kds))
        {
            tk_autotune_set_steps(kps, kis, kds);
            at_reply_status("AUTO_STEP");
            return 1U;
        }
    }

    /* AUTO SCORE <value> */
    {
        float score;
        if (1 == sscanf(line, "AUTO SCORE %f", &score))
        {
            if (tk_autotune_submit_score(score))
            {
                at_reply_status("AUTO_SCORE");
            }
            else
            {
                tk_reply_error("AUTO_SCORE", "STATE_INVALID");
            }
            return 1U;
        }
    }

    return 0U;
}

void tk_autotune_register_commands(void)
{
    tk_register_command("AUTO ", tk_autotune_cmd_handler);
}
