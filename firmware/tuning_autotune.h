/*
 * tuning_autotune.h
 *
 * SmartCar Tuning Kit - PID auto-tuning via 7-candidate grid search.
 * Adapted from autotune.h.
 */

#ifndef TUNING_AUTOTUNE_H_
#define TUNING_AUTOTUNE_H_

#include <stdint.h>
#include "tuning_pid.h"

typedef enum
{
    TK_AUTOTUNE_IDLE = 0U,
    TK_AUTOTUNE_RUNNING,
    TK_AUTOTUNE_WAIT_SCORE,
    TK_AUTOTUNE_DONE,
    TK_AUTOTUNE_ABORTED,
    TK_AUTOTUNE_ERROR,
} tk_autotune_state_t;

typedef struct
{
    tk_pid_param_t      base;
    tk_pid_param_t      current;
    tk_pid_param_t      best;
    float               kp_step;
    float               ki_step;
    float               kd_step;
    float               best_score;
    float               last_score;
    uint16_t            candidate_index;
    uint16_t            candidate_total;
    uint8_t             best_valid;
    tk_autotune_state_t state;
    uint32_t            started_ms;
    uint32_t            updated_ms;
} tk_autotune_status_t;

void                        tk_autotune_init(void);
void                        tk_autotune_reset(void);
uint8_t                     tk_autotune_start(const tk_pid_param_t *base);
void                        tk_autotune_stop(void);
uint8_t                     tk_autotune_is_active(void);
tk_autotune_state_t         tk_autotune_get_state(void);
const tk_autotune_status_t *tk_autotune_get_status(void);
uint8_t                     tk_autotune_submit_score(float score);
uint8_t                     tk_autotune_apply_best(uint8_t save_to_flash);
void                        tk_autotune_set_steps(float kp_step, float ki_step, float kd_step);

/* Register AUTO commands with the framework (called internally by tk_init). */
void tk_autotune_register_commands(void);

#endif /* TUNING_AUTOTUNE_H_ */
