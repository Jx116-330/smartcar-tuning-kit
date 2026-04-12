/*
 * tuning_kit_config.h
 *
 * SmartCar Tuning Kit - Compile-time configuration
 * Copy this file into your project and edit as needed.
 */

#ifndef TUNING_KIT_CONFIG_H_
#define TUNING_KIT_CONFIG_H_

/* ---------- Buffer sizes ---------- */
#define TK_SEND_BUFFER_LEN      384U    /* Max bytes per telemetry line       */
#define TK_RECV_BUFFER_LEN      192U    /* Max bytes per received command     */
#define TK_RESP_BUFFER_LEN      256U    /* Max bytes per response line        */

/* ---------- Registration limits ---------- */
#define TK_MAX_TEL_CHANNELS     8U      /* Max registered telemetry builders  */
#define TK_MAX_CMD_HANDLERS     16U     /* Max registered command handlers    */

/* ---------- Telemetry timing ---------- */
#define TK_DEFAULT_PERIOD_MS    100U    /* Default telemetry send interval    */
#define TK_MIN_PERIOD_MS        50U     /* Minimum allowed period             */
#define TK_MAX_PERIOD_MS        1000U   /* Maximum allowed period             */
#define TK_PERIOD_STEP_MS       50U     /* Period increment step              */

#endif /* TUNING_KIT_CONFIG_H_ */
