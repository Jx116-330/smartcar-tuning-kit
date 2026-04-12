/*
 * tuning_pid.h
 *
 * SmartCar Tuning Kit - PID types, inline controller, and PID callbacks.
 * Adapted from pid_runtime.h.
 */

#ifndef TUNING_PID_H_
#define TUNING_PID_H_

#include <stdint.h>
#include <stddef.h>

/* ===================== PID parameter type ===================== */

typedef struct
{
    float kp;
    float ki;
    float kd;
    float integral_limit;
    float output_limit;
} tk_pid_param_t;

/* ===================== PID controller state ===================== */

typedef struct
{
    tk_pid_param_t param;
    float integral;
    float last_error;
    float output;
} tk_pid_controller_t;

/* ===================== Inline PID helpers ===================== */

static inline float tk_pid_limit(float value, float limit)
{
    if (limit <= 0.0f) return value;
    if (value > limit)  return limit;
    if (value < -limit) return -limit;
    return value;
}

static inline void tk_pid_set_param(tk_pid_controller_t *c, const tk_pid_param_t *p)
{
    if ((NULL == c) || (NULL == p)) return;
    c->param = *p;
}

static inline void tk_pid_reset(tk_pid_controller_t *c)
{
    if (NULL == c) return;
    c->integral   = 0.0f;
    c->last_error = 0.0f;
    c->output     = 0.0f;
}

static inline float tk_pid_calculate(tk_pid_controller_t *c, float target, float feedback)
{
    float error, derivative;
    if (NULL == c) return 0.0f;

    error = target - feedback;
    c->integral += error;
    c->integral = tk_pid_limit(c->integral, c->param.integral_limit);

    derivative = error - c->last_error;
    c->last_error = error;

    c->output = c->param.kp * error
              + c->param.ki * c->integral
              + c->param.kd * derivative;
    c->output = tk_pid_limit(c->output, c->param.output_limit);
    return c->output;
}

/* ===================== PID access callbacks ===================== */

/*
 * The user implements these two functions to connect the tuning kit
 * to their PID parameter storage (e.g. flash, global struct, menu system).
 */
typedef struct
{
    /* Return pointer to current PID params (must remain valid until next call).
     * Return NULL if unavailable. */
    const tk_pid_param_t* (*get_param)(void);

    /* Apply new PID params. If save_to_flash != 0, persist to non-volatile storage.
     * Return 1 on success, 0 on failure. */
    uint8_t (*set_param)(const tk_pid_param_t *param, uint8_t save_to_flash);
} tk_pid_callbacks_t;

/* Register PID GET/SET/SAVE commands with the framework (called internally by tk_init). */
void tk_pid_register_commands(void);

#endif /* TUNING_PID_H_ */
