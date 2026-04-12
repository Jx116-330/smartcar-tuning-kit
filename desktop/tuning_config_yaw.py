"""
SmartCar Tuning Tool - Yaw Tuning Configuration.

Drop-in replacement for tuning_config.py that adds yaw-specific UI.
To use: rename this file to tuning_config.py (or copy its content over).
"""
import math

# ==================== Application ====================
APP_TITLE = 'SmartCar Yaw Tuning Tool'
HTTP_HOST = '127.0.0.1'
HTTP_PORT = 9898

# ==================== Telemetry Packets ====================
TELEMETRY_PREFIXES = ('TEL', 'TELG', 'TELA', 'TELY')
RESPONSE_PREFIXES = ('ACK', 'ERR', 'YAWCAL', 'YAWTEST')

# ==================== TELY Short Key Expansion ====================
KEY_MAP = {
    'gzr': 'gz_raw',   'gzb': 'gz_bias',  'gzc': 'gz_comp',
    'gzs': 'gz_scale', 'gzd': 'gz_scaled',
    'yi': 'yaw_int',   'yf': 'yaw_final',  'yc': 'yaw_corr',
    'dt': 'dt_us',     'dbg': 'yaw_dbg',   'ver': 'yaw_param_ver',
    'ton': 'yaw_test_on',  'tst': 'yaw_test_start', 'tdl': 'yaw_test_last_delta',
    'zb': 'yaw_zero_busy', 'zok': 'yaw_zero_ok',
    'zn': 'yaw_zero_n',    'zgz': 'yaw_zero_gbz',
}

# ==================== Plot Channels ====================
PLOT_KEYS = [
    ('gx',        '#4fc3f7', True),
    ('gy',        '#81c784', True),
    ('gz',        '#e57373', True),
    ('gxyz',      '#ba68c8', True),
    ('gz_comp',   '#ffd54f', False),
    ('gz_scaled', '#ff8a65', False),
    ('yaw_final', '#ce93d8', False),
]

# ==================== Metric Cards ====================
PRIMARY_METRICS = [
    ('gx', 'GX'), ('gy', 'GY'), ('gz', 'GZ'), ('gxyz', 'GXYZ'),
]

DETAIL_METRICS = [
    ('roll', 'ROLL'), ('pitch', 'PITCH'), ('yaw', 'YAW'),
    ('anorm', 'ANORM'),
    ('bias_ok', 'BIAS_OK'), ('bias_cal', 'BIAS_CAL'),
    ('bias_n', 'BIAS_N'), ('bias_t', 'BIAS_T'),
]

EXTENDED_METRICS = [
    ('gcx', 'GCX'), ('gcy', 'GCY'), ('gcz', 'GCZ'),
    ('gbx', 'GBX'), ('gby', 'GBY'), ('gbz', 'GBZ'),
    ('bias_flash', 'FLASH'),
    ('q0', 'Q0'), ('q1', 'Q1'), ('q2', 'Q2'), ('q3', 'Q3'),
    ('ax', 'AX'), ('ay', 'AY'), ('az', 'AZ'), ('anorm', 'ANORM'),
    ('att_upd', 'ATT_UPD'),
]

# ==================== Quick Commands ====================
QUICK_COMMANDS = [
    {'label': 'GET PID',  'command': 'GET PID'},
    {'label': 'SAVE PID', 'command': 'SAVE PID'},
    {'label': 'GET YAW',  'command': 'GET YAW'},
]

# ==================== Yaw Status Tab ====================
CUSTOM_TABS = [
    {
        'name': 'Yaw Status',
        'fields': [
            ('gz_raw',   'GZ_RAW'),
            ('gz_bias',  'GZ_BIAS'),
            ('gz_comp',  'GZ_COMP'),
            ('gz_scale', 'GZ_SCALE'),
            ('gz_scaled','GZ_SCALED'),
            ('yaw_int',  'YAW_INT'),
            ('yaw_final','YAW_FINAL'),
            ('yaw_corr', 'YAW_CORR'),
            ('dt_us',    'DT_US'),
            ('yaw_param_ver', 'PARAM_VER'),
            ('yaw_test_on',   'TEST_ON'),
            ('yaw_test_start','TEST_START'),
            ('yaw_test_last_delta', 'TEST_DELTA'),
            ('yaw_zero_busy', 'ZERO_BUSY'),
            ('yaw_zero_ok',   'ZERO_OK'),
            ('yaw_zero_n',    'ZERO_N'),
            ('yaw_zero_gbz',  'ZERO_GBZ'),
        ],
        'result_keys': [
            ('ACK',     'yaw_last_ack'),
            ('YAWCAL',  'yaw_last_cal'),
            ('YAWTEST', 'yaw_last_test'),
        ],
    },
]

# ==================== Yaw Command Tab ====================
COMMAND_TABS = [
    {
        'name': 'Yaw Cmds',
        'buttons': [
            {'label': 'GET YAW',         'command': 'GET YAW'},
            {'label': 'YAW ZERO (1s)',   'command': 'YAW ZERO'},
            {'label': 'YAW TEST START',  'command': 'YAW TEST START'},
            {'label': 'YAW TEST STOP',   'command': 'YAW TEST STOP'},
            {'label': 'YAW ZERO STATUS', 'command': 'YAW ZERO STATUS'},
        ],
        'params': [
            {'label': 'YAW_BIAS (dps)', 'prefix': 'SET YAW_BIAS'},
            {'label': 'YAW_SCALE',      'prefix': 'SET YAW_SCALE'},
        ],
    },
]

# ==================== Custom State File ====================
CUSTOM_STATE_FILE = 'latest_yaw_state.json'

# ==================== Optional UI Settings ====================
# MAX_PLOT_POINTS = 2000      # Max stored data points (default 2000)
# ACCENT_COLOR = '#7c3aed'    # Override accent color

# ==================== Simulation ====================
def build_simulated_packet(tick):
    t = tick * 0.1
    gx = 0.8 * math.sin(t / 8.0)
    gy = 1.2 * math.cos(t / 10.0)
    gz = 8.0 * math.sin(t / 12.0)
    gxyz = math.sqrt(gx**2 + gy**2 + gz**2)
    yaw = 45.0 * math.sin(t / 25.0)
    ms = tick * 100
    return (
        f"TELG,ms={ms},gx={gx:.3f},gy={gy:.3f},gz={gz:.3f},"
        f"gcx={gx*0.9:.3f},gcy={gy*0.9:.3f},gcz={gz*0.9:.3f},gxyz={gxyz:.3f},"
        f"gbx=0.01,gby=-0.02,gbz=0.03,"
        f"bias_ok=1,bias_cal=0,bias_n=20000,bias_t=20000,bias_flash=1,"
        f"roll=0.0,pitch=0.0,yaw={yaw:.2f},"
        f"q0=1.0,q1=0.0,q2=0.0,q3=0.0,"
        f"ax=0.01,ay=-0.02,az=1.00,anorm=1.00,att_upd={ms}"
    )

def status_banner_logic(parsed):
    return None
