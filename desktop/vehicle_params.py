"""
vehicle_params.py - centralized physical constants for the T-kart platform.

Single source of truth for everything the offline simulator and any future
MATLAB model need to know about the vehicle. When a value is updated here, all
consumers pick up the change on next run.

Vehicle: 科宇科技 T 型卡丁车 V1.2 (T-shape go-kart).

============================================================================
Source provenance (2026-05-11)
----------------------------------------------------------------------------
Each value below is tagged with where it came from:

  DATASHEET  - 科宇科技T型卡丁车资料合集V1.2.pdf (manufacturer-supplied,
               'hand-measured with slight error' per the PDF's own note).
               Treat as authoritative until contradicted by physical
               re-measurement.

  FIRMWARE   - extracted from C firmware sources
               (ins_ctrl.h / ins_actuator.h / tuning_dispatch.c).
               These are verifiably true at the code level; they may or
               may not match the physical car (e.g. STEER_LIMIT_DEG=29
               is the firmware clamp, not the mechanical end-stop).

  USER_SIM   - taken from E:/ads/ackermann_steering_sim.m and
               E:/ads/subject1_sim.m. The user has confirmed these scripts
               use SOME synthetic values not measured from the platform.
               Treat USER_SIM as "starting estimate, unverified".

  UNKNOWN    - not in any source yet; needs user confirmation or physical
               measurement.

  PLACEHOLDER - rough order-of-magnitude guess that needs replacement.
============================================================================
"""
from __future__ import annotations

from dataclasses import dataclass


# ============================================================================
# Kinematic geometry (科宇科技 T-kart V1.2)
# ============================================================================

# Distance between front and rear axles. Originally USER_SIM 0.80m from
# E:/ads/ackermann_steering_sim.m. **FITTED 2026-05-14** to 0.957 m by
# calibrate_vehicle_params.py from 4 engaged recordings (336 cornering
# frames; per-file medians spanned 0.944 .. 0.995, IQR per file 0.91-1.01).
# This is the EFFECTIVE wheelbase the kinematic-bicycle simulator should
# use - it includes any constant scaling between commanded steer (`str`
# in TELMISSION) and actual wheel angle, so it's exactly the value that
# makes simulated yaw rate match measured yaw rate. A future tape-measure
# of the geometric wheelbase may differ; if so, the delta tells us the
# steer-cmd-to-wheel-angle scale factor.
WHEELBASE_M = 0.957          # FITTED (by calibrate_vehicle_params.py from real recordings)

# Front track width: distance between LEFT and RIGHT front wheel centers.
# PDF p.1: "前轮间距 50CM (手工测量, 略有误差)".
TRACK_FRONT_M = 0.50        # DATASHEET

# Rear track width: distance between LEFT and RIGHT rear wheel centers.
# PDF p.1: "后轮间距 50CM (手工测量, 略有误差)".
TRACK_REAR_M = 0.50         # DATASHEET

# Legacy alias kept for backward-compat with any code still importing TRACK_M.
TRACK_M = TRACK_FRONT_M

# Wheel radii: PDF p.1 lists FRONT and REAR diameters SEPARATELY.
# Front: 20cm diameter, 8.5cm width.  Rear: 24cm diameter, 8cm width.
# The rear wheels are taller than the front - matters for encoder->speed
# conversion (rear-wheel encoders).
FRONT_WHEEL_RADIUS_M = 0.10 # DATASHEET (20cm diameter / 2)
REAR_WHEEL_RADIUS_M  = 0.12 # DATASHEET (24cm diameter / 2)
FRONT_WHEEL_WIDTH_M  = 0.085 # DATASHEET
REAR_WHEEL_WIDTH_M   = 0.080 # DATASHEET

# Convenience alias - use this only when a generic "wheel radius" is needed.
# Defaults to REAR because rear wheels carry the odometry encoders.
WHEEL_RADIUS_M = REAR_WHEEL_RADIUS_M

# Center-of-gravity distance from rear axle. Source: ackermann_steering_sim.m
# l_cg=0.35m (synthetic, comment says "estimated ~0.44*L"). Used for cg-
# trajectory plotting only; kinematic bicycle tracks rear-axle midpoint.
CG_TO_REAR_M = 0.35         # USER_SIM

# Steering trapezoid arm length. Source: ackermann_steering_sim.m l_arm=0.08m
# (synthetic, comment says "estimated, scaled by front-track ratio").
STEER_ARM_M = 0.08          # USER_SIM

# Minimum turning radius from pure Ackermann kinematics:
#   R_min = WHEELBASE / tan(delta_max)
# Two flavours (see Steering section below for the angle constants):
#   MIN_TURN_RADIUS_M             - what the simulator actually uses
#                                   (firmware clamp 29deg)
#   MIN_TURN_RADIUS_MECHANICAL_M  - what the wheels could physically do
#                                   if firmware allowed it (30deg)
# Both depend on WHEELBASE_M which is still USER_SIM, so treat as ordering-
# only until tape-measure check. Hardcoding the angles here so the file
# remains import-order-safe.
import math as _math
MIN_TURN_RADIUS_M            = WHEELBASE_M / _math.tan(_math.radians(29.0))
MIN_TURN_RADIUS_MECHANICAL_M = WHEELBASE_M / _math.tan(_math.radians(30.0))


# ============================================================================
# Steering
# ============================================================================

# Hard limit on commanded steer angle from firmware ins_actuator.h L43:
#   #define INS_ACT_STEER_LIMIT_DEG  29.0f
# User confirmed 2026-05-12: mechanical max is 30 deg (MAX_STEER_PHYSICAL_DEG
# below). Firmware therefore reserves 1 deg of margin to avoid end-stop.
STEER_LIMIT_DEG = 29.0      # FIRMWARE (active clamp used by simulate())

# Mechanical maximum steering angle of the wheels themselves, INDEPENDENT of
# the firmware clamp above. The simulator should stay <= STEER_LIMIT_DEG, but
# downstream tools (path optimizer, R_min computation) may want the true
# mechanical limit for headroom analysis.
MAX_STEER_PHYSICAL_DEG = 30.0  # USER (confirmed 2026-05-12)

# Servo first-order time constant. The Turn PID closes around the servo
# encoder so the *effective* steer response is faster than open-loop, but
# the simulator currently does not model the Turn loop at all — it commands
# the angle and the kinematic bicycle uses it immediately. To approximate
# real behavior we would lag the commanded angle by SERVO_TAU_S. Not used
# until simulate() grows a first-order steer-tracking model.
SERVO_TAU_S = 0.05          # PLACEHOLDER

# Steer angle deadband (deg). Below this the servo does not move noticeably.
# Used when modeling jitter / discretization.
STEER_DEADBAND_DEG = 0.5    # PLACEHOLDER


# ============================================================================
# Drive (longitudinal)
# ============================================================================

# Default SPEED_CAP from ins_actuator.h, in mm/s.
SPEED_CAP_DEFAULT_MMS = 400 # MEASURED (source code default)

# Hard upper bound on commanded speed via the SET MISSION SPEED_CAP path.
# tuning_dispatch.c clamps SPEED_CAP to [0, 1500].
SPEED_CAP_MAX_MMS = 1500    # MEASURED (firmware clamp)

# Nominal cruise speed used by the Ackermann sim. Not a measurement.
V_NOM_PHYSICAL_MS = 2.0     # USER_SIM

# ----------------------------------------------------------------------
# Drive motor parameters (from PDF p.2 "车模后轮行进电机")
# Useful for sanity-checking max attainable speeds and ramp limits.
# ----------------------------------------------------------------------
REAR_MOTOR_GEAR_RATIO     = 18.0    # DATASHEET (gearbox 1:18)
REAR_MOTOR_RATED_V        = 24.0    # DATASHEET (V)
REAR_MOTOR_RATED_TORQUE_NM = 0.37   # DATASHEET (N.m, rated load)
REAR_MOTOR_NOLOAD_RPM     = 5800.0  # DATASHEET (5800 RPM ±7.5%)
REAR_MOTOR_LOAD_RPM       = 5000.0  # DATASHEET (5000 RPM ±7.5%)
REAR_MOTOR_NOLOAD_CURRENT_A = 1.1   # DATASHEET
REAR_MOTOR_LOAD_CURRENT_MAX_A = 10.5 # DATASHEET

# Derived rear-wheel speeds (after 1:18 gearbox, with rear wheel radius):
#   ω_wheel_rpm = motor_rpm / gear_ratio
#   v_wheel_ms  = ω_wheel_rpm * 2π * R_rear / 60
# At no-load: 5800/18 = 322 RPM -> 4.05 m/s on 0.12m wheels
# At rated load: 5000/18 = 278 RPM -> 3.49 m/s
V_MAX_NOLOAD_MS = (REAR_MOTOR_NOLOAD_RPM / REAR_MOTOR_GEAR_RATIO) \
                  * 2 * _math.pi * REAR_WHEEL_RADIUS_M / 60.0  # ~4.05 m/s
V_MAX_LOADED_MS = (REAR_MOTOR_LOAD_RPM / REAR_MOTOR_GEAR_RATIO) \
                  * 2 * _math.pi * REAR_WHEEL_RADIUS_M / 60.0  # ~3.49 m/s

# ----------------------------------------------------------------------
# Steering motor (from PDF p.1 "车模转向电机")
# ----------------------------------------------------------------------
STEER_MOTOR_GEAR_RATIO = 192.0      # DATASHEET (gearbox 1:192)
# Steering hall encoder: dual hall (AB quadrature) per PDF p.3. Pulse count
# per motor revolution depends on hall pole-pair; needs verification.
STEER_HALL_QUADRATURE  = True       # DATASHEET (dual-hall AB)

# Slow-down ratio when within CRUISE_DIST_M of the cursor target.
# ins_ctrl.h L9: INS_CTRL_SLOW_SPEED_RATIO = 0.50f
SLOW_RATIO = 0.50           # MEASURED
CRUISE_DIST_M = 0.60        # MEASURED (ins_ctrl.h L8)

# Maximum lateral acceleration before tires slip. Used by speed planner
# (Stage 3) to derive v_max(curvature) = sqrt(a_lat_max / curvature).
# Hobby-scale TT01 chassis on indoor floor, conservative guess.
#
# **DEMONSTRATED LOWER BOUND** (calibrate_vehicle_params.py 2026-05-14):
# 0.082 m/s^2 (p95 across all engaged frames in recordings/, max-of-max
# 0.098). All recordings were at v_max <= 0.47 m/s — far below the chassis
# limit — so the demonstrated value heavily underbids the physical limit.
# Keep PLACEHOLDER 3.0 here until a focused high-speed corner-ramp test
# (record while sweeping cornering speed up to slip) gives a real upper
# bound. Run that test and re-run calibrate_vehicle_params.py with
# --apply-accels to update.
MAX_LATERAL_ACCEL_MS2 = 3.0    # PLACEHOLDER (demonstrated lower bound 0.082)


# ============================================================================
# Lookahead / Pure Pursuit
# ============================================================================

# Minimum geometric lookahead distance. Below this the simulator drops the
# atan2 geometry term and follows recorded yaw instead (ins_ctrl.h L13).
LA_GEO_MIN_M = 0.15         # MEASURED

# Reach threshold to advance the playback cursor. ins_playback.h.
REACH_THRESHOLD_M = 0.20    # MEASURED


# ============================================================================
# Lost-path watchdog
# ============================================================================

# Distance threshold above which the lost-path watchdog starts counting.
LOST_DIST_M = 2.0           # MEASURED (ins_actuator.h L54)

# Watchdog tick rate (10ms) -> 1 second hold time.
LOST_HOLD_TICKS = 100       # MEASURED


# ============================================================================
# Encoder odometry
# ============================================================================

# Encoder count -> millimeter conversion. Both wheels assumed identical.
# Placeholder; calibrate by pushing the car a known distance and reading
# TELODOM.enc_dist_mm at both wheels.
ENC_TICKS_PER_MM_L = 1.0    # PLACEHOLDER
ENC_TICKS_PER_MM_R = 1.0    # PLACEHOLDER


# ============================================================================
# Noise / non-ideality model parameters  (ALL PLACEHOLDER)
# ============================================================================
# These let sim_pure_pursuit add realistic sensor / actuator imperfections
# to its output. NONE of these have been measured from the actual kart -
# the order of magnitudes below are educated guesses informed by the
# memory entries about rear-encoder vibration ("vibration pollutes PID")
# and magnetometer ellipse calibration. They are deliberately conservative
# enough to show up in XTE rms without dominating the trace.
#
# The simulator imports these but does NOT use them unless the caller
# explicitly opts in via Params(enable_*_noise=True). Default is OFF so
# the deterministic (Stage 1.5) behavior stays reproducible.

# Position estimate noise. Gaussian zero-mean noise added to (cur_x, cur_y)
# AS SEEN BY THE CONTROLLER (not by the kinematic-bicycle integrator).
# Simulates encoder discretization + vibration-induced miscounts. The
# magnitude is per-step; correlation length is 1 tick (white noise).
# Reasonable guess: ~5 mm std at the rear-wheel scale.
ENCODER_POSITION_NOISE_STD_M = 0.005   # PLACEHOLDER

# Yaw estimate bias + Gaussian noise. Simulates magnetometer ellipse
# residue (bias) and electrical noise (random component). The bias is
# constant across a single run; the random part is white per-step.
# Reasonable starting guesses: 1deg bias, 0.5deg std random.
MAGNETO_YAW_BIAS_DEG       = 1.0       # PLACEHOLDER
MAGNETO_YAW_NOISE_STD_DEG  = 0.5       # PLACEHOLDER

# Servo deadband: |steer_cmd| < this value produces no servo motion at
# all. Simulates the dead zone where the brushed DC servo cannot
# overcome static friction. The Turn PID typically masks this on a real
# car, but the simulator's actuator skips Turn entirely and writes
# directly to a kinematic steer, so without modeling deadband the sim
# over-rewards micro corrections.
SERVO_DEADBAND_DEG = 0.5               # PLACEHOLDER

# Servo backlash: extra deg the steer must travel through 0 before
# reversing direction (mechanical gear play). Modeled as one-shot per
# direction change.
SERVO_BACKLASH_DEG = 0.3               # PLACEHOLDER


# ============================================================================
# Simulation step
# ============================================================================

# Simulator integration step. Matches the 10ms actuator tick used by the
# firmware (`ins_actuator_step` is called from a 10ms RTOS task).
SIM_DT_S = 0.01             # MEASURED


# ============================================================================
# Aggregate view (for diagnostic prints)
# ============================================================================

@dataclass(frozen=True)
class VehicleParams:
    wheelbase_m:         float = WHEELBASE_M
    track_front_m:       float = TRACK_FRONT_M
    track_rear_m:        float = TRACK_REAR_M
    front_wheel_radius_m: float = FRONT_WHEEL_RADIUS_M
    rear_wheel_radius_m: float = REAR_WHEEL_RADIUS_M
    cg_to_rear_m:        float = CG_TO_REAR_M
    min_turn_radius_m:   float = MIN_TURN_RADIUS_M
    steer_limit_deg:     float = STEER_LIMIT_DEG
    speed_cap_max_mms:   int   = SPEED_CAP_MAX_MMS
    v_nom_physical_ms:   float = V_NOM_PHYSICAL_MS
    v_max_noload_ms:     float = V_MAX_NOLOAD_MS
    v_max_loaded_ms:     float = V_MAX_LOADED_MS
    slow_ratio:          float = SLOW_RATIO
    cruise_dist_m:       float = CRUISE_DIST_M
    la_geo_min_m:        float = LA_GEO_MIN_M
    reach_threshold_m:   float = REACH_THRESHOLD_M
    lost_dist_m:         float = LOST_DIST_M
    lost_hold_ticks:     int   = LOST_HOLD_TICKS
    sim_dt_s:            float = SIM_DT_S


def print_summary() -> None:
    """Diagnostic dump - one line per parameter with provenance tag."""
    rows = [
        # geometry
        ("WHEELBASE_M",            WHEELBASE_M,            "USER_SIM  (PDF doesn't list; needs check)"),
        ("TRACK_FRONT_M",          TRACK_FRONT_M,          "DATASHEET"),
        ("TRACK_REAR_M",           TRACK_REAR_M,           "DATASHEET"),
        ("FRONT_WHEEL_RADIUS_M",   FRONT_WHEEL_RADIUS_M,   "DATASHEET"),
        ("REAR_WHEEL_RADIUS_M",    REAR_WHEEL_RADIUS_M,    "DATASHEET"),
        ("FRONT_WHEEL_WIDTH_M",    FRONT_WHEEL_WIDTH_M,    "DATASHEET"),
        ("REAR_WHEEL_WIDTH_M",     REAR_WHEEL_WIDTH_M,     "DATASHEET"),
        ("CG_TO_REAR_M",           CG_TO_REAR_M,           "USER_SIM"),
        ("MIN_TURN_RADIUS_M",      round(MIN_TURN_RADIUS_M, 3),      "DERIVED (WB x 29deg firmware clamp)"),
        ("MIN_TURN_RADIUS_MECHANICAL_M", round(MIN_TURN_RADIUS_MECHANICAL_M, 3), "DERIVED (WB x 30deg mech max)"),
        # steering / drive limits
        ("STEER_LIMIT_DEG",        STEER_LIMIT_DEG,        "FIRMWARE (active clamp)"),
        ("MAX_STEER_PHYSICAL_DEG", MAX_STEER_PHYSICAL_DEG, "USER (confirmed 2026-05-12)"),
        ("SERVO_TAU_S",            SERVO_TAU_S,            "PLACEHOLDER"),
        ("SPEED_CAP_DEFAULT_MMS",  SPEED_CAP_DEFAULT_MMS,  "FIRMWARE"),
        ("SPEED_CAP_MAX_MMS",      SPEED_CAP_MAX_MMS,      "FIRMWARE"),
        ("V_NOM_PHYSICAL_MS",      V_NOM_PHYSICAL_MS,      "USER_SIM"),
        ("V_MAX_NOLOAD_MS",        round(V_MAX_NOLOAD_MS, 3), "DERIVED from motor+wheel"),
        ("V_MAX_LOADED_MS",        round(V_MAX_LOADED_MS, 3), "DERIVED from motor+wheel"),
        # motor / gearbox
        ("REAR_MOTOR_GEAR_RATIO",  REAR_MOTOR_GEAR_RATIO,  "DATASHEET (1:18)"),
        ("REAR_MOTOR_NOLOAD_RPM",  REAR_MOTOR_NOLOAD_RPM,  "DATASHEET (±7.5%)"),
        ("REAR_MOTOR_LOAD_RPM",    REAR_MOTOR_LOAD_RPM,    "DATASHEET (±7.5%)"),
        ("REAR_MOTOR_RATED_TORQUE_NM", REAR_MOTOR_RATED_TORQUE_NM, "DATASHEET"),
        ("STEER_MOTOR_GEAR_RATIO", STEER_MOTOR_GEAR_RATIO, "DATASHEET (1:192)"),
        # control law constants
        ("SLOW_RATIO",             SLOW_RATIO,             "FIRMWARE"),
        ("CRUISE_DIST_M",          CRUISE_DIST_M,          "FIRMWARE"),
        ("LA_GEO_MIN_M",           LA_GEO_MIN_M,           "FIRMWARE"),
        ("REACH_THRESHOLD_M",      REACH_THRESHOLD_M,      "FIRMWARE"),
        ("LOST_DIST_M",            LOST_DIST_M,            "FIRMWARE"),
        ("LOST_HOLD_TICKS",        LOST_HOLD_TICKS,        "FIRMWARE"),
        # placeholders for things we cannot guess
        ("MAX_LATERAL_ACCEL_MS2",  MAX_LATERAL_ACCEL_MS2,  "PLACEHOLDER"),
        ("ENC_TICKS_PER_MM_L",     ENC_TICKS_PER_MM_L,     "PLACEHOLDER"),
        ("ENC_TICKS_PER_MM_R",     ENC_TICKS_PER_MM_R,     "PLACEHOLDER"),
        # noise model
        ("ENCODER_POSITION_NOISE_STD_M", ENCODER_POSITION_NOISE_STD_M, "PLACEHOLDER"),
        ("MAGNETO_YAW_BIAS_DEG",         MAGNETO_YAW_BIAS_DEG,         "PLACEHOLDER"),
        ("MAGNETO_YAW_NOISE_STD_DEG",    MAGNETO_YAW_NOISE_STD_DEG,    "PLACEHOLDER"),
        ("SERVO_DEADBAND_DEG",           SERVO_DEADBAND_DEG,           "PLACEHOLDER"),
        ("SERVO_BACKLASH_DEG",           SERVO_BACKLASH_DEG,           "PLACEHOLDER"),
        ("SIM_DT_S",               SIM_DT_S,               "FIRMWARE (matches 10ms tick)"),
    ]
    width = max(len(n) for n, _, _ in rows)
    for name, value, status in rows:
        print(f"  {name:<{width}} = {value!s:>10}  [{status}]")


if __name__ == "__main__":
    print("Vehicle parameters in use:")
    print_summary()
