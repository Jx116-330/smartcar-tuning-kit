/*
 * tuning_yaw.h
 *
 * SmartCar Tuning Kit - Yaw tuning example.
 * Demonstrates how to register a custom telemetry channel (TELY)
 * and custom commands (GET YAW, YAW ZERO, YAW TEST, SET YAW_BIAS/SCALE).
 *
 * Usage:
 *   1. Implement tk_yaw_callbacks_t for your attitude estimator.
 *   2. Call tk_yaw_init(&cbs) AFTER tk_init().
 *   3. The TELY packet and YAW commands are automatically registered.
 */

#ifndef TUNING_YAW_H_
#define TUNING_YAW_H_

#include <stdint.h>

/* ===================== Yaw access callbacks ===================== */

typedef struct
{
    /* Get yaw debug data. Any pointer can be NULL to skip. */
    void (*get_debug)(float *gz_raw, float *gz_bias, float *gz_comp,
                      float *gz_scaled, float *yaw_integral,
                      float *yaw_final, float *yaw_correction, float *yaw_dt_s);

    /* Get zero-calibration state. Any pointer can be NULL. */
    void (*get_zero_state)(uint8_t *busy, uint8_t *ok, uint32_t *n, float *gbz);

    /* Scale factor get/set */
    float (*get_scale)(void);
    void  (*set_scale)(float s);

    /* Manual bias get/set (dps) */
    float (*get_manual_bias)(void);
    void  (*set_manual_bias)(float bias_dps);

    /* Debug enable get/set */
    uint8_t (*get_debug_enable)(void);
    void    (*set_debug_enable)(uint8_t en);

    /* Parameter version counter */
    uint32_t (*get_param_version)(void);

    /* Start zero calibration (sample_count points) */
    void (*zero_start)(uint32_t sample_count);

    /* 90-degree rotation test */
    void  (*test_start)(void);
    void  (*test_stop)(float *start_deg, float *end_deg, float *delta, float *err90);
    uint8_t (*test_is_on)(void);
    float (*test_get_start)(void);
    float (*test_get_last_delta)(void);
} tk_yaw_callbacks_t;

/* Initialize yaw tuning: registers TELY telemetry and YAW commands. */
void tk_yaw_init(const tk_yaw_callbacks_t *cbs);

#endif /* TUNING_YAW_H_ */
