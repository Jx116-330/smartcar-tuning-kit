"""
SmartCar Tuning Tool - Configuration file.

Edit this file to customize the tool for YOUR project.
All domain-specific knowledge lives here; the framework (tuning_tool.py) reads it.
"""
import math

# ==================== Application ====================
APP_TITLE = 'SmartCar Tuning Tool'
HTTP_HOST = '127.0.0.1'
HTTP_PORT = 9898

# ==================== Telemetry Packets ====================
# Packet prefixes that the tool recognizes as telemetry data.
TELEMETRY_PREFIXES = ('TEL', 'TELG', 'TELA')

# Response packet prefixes (ACK, ERR, and any custom response types).
RESPONSE_PREFIXES = ('ACK', 'ERR')

# ==================== Short Key Expansion ====================
# Map short keys (sent over TCP for bandwidth) to long display keys.
# Leave empty if your protocol already uses full key names.
KEY_MAP = {}

# ==================== Plot Channels ====================
# (data_key, hex_color, default_visible)
PLOT_KEYS = [
    ('gx',   '#4fc3f7', True),
    ('gy',   '#81c784', True),
    ('gz',   '#e57373', True),
    ('gxyz', '#ba68c8', True),
]

# ==================== Metric Cards ====================
# Primary metrics: shown as large cards at the top.
# (data_key, display_label)
PRIMARY_METRICS = [
    ('gx', 'GX'), ('gy', 'GY'), ('gz', 'GZ'), ('gxyz', 'GXYZ'),
]

# Detail metrics: shown as compact key-value rows.
DETAIL_METRICS = [
    ('roll', 'ROLL'), ('pitch', 'PITCH'), ('yaw', 'YAW'),
    ('anorm', 'ANORM'),
    ('bias_ok', 'BIAS_OK'), ('bias_cal', 'BIAS_CAL'),
    ('bias_n', 'BIAS_N'), ('bias_t', 'BIAS_T'),
]

# Extended metrics: shown in a scrollable area.
EXTENDED_METRICS = [
    ('gcx', 'GCX'), ('gcy', 'GCY'), ('gcz', 'GCZ'),
    ('gbx', 'GBX'), ('gby', 'GBY'), ('gbz', 'GBZ'),
    ('bias_flash', 'FLASH'),
    ('q0', 'Q0'), ('q1', 'Q1'), ('q2', 'Q2'), ('q3', 'Q3'),
    ('ax', 'AX'), ('ay', 'AY'), ('az', 'AZ'), ('anorm', 'ANORM'),
    ('att_upd', 'ATT_UPD'),
]

# ==================== Quick Commands ====================
# Buttons shown in the sidebar for one-click sending.
QUICK_COMMANDS = [
    {'label': 'GET PID',  'command': 'GET PID'},
    {'label': 'SAVE PID', 'command': 'SAVE PID'},
]

# ==================== Custom Status Tabs ====================
# Each entry creates a tab on the right panel with live-updating fields.
# {
#     'name':   'Tab Name',
#     'fields': [(data_key, label), ...],      # Fields to display
#     'key_map': {short: long, ...},            # Optional per-tab key alias
#     'result_keys': [('ACK', var_key), ...],   # Optional result display
# }
CUSTOM_TABS = []

# ==================== Command Tabs ====================
# Each entry creates a tab with command buttons and parameter inputs.
# {
#     'name':    'Tab Name',
#     'buttons': [{'label': '...', 'command': '...'}, ...],
#     'params':  [{'label': '...', 'prefix': 'SET XXX'}, ...],
# }
COMMAND_TABS = []

# ==================== Custom State File ====================
# Additional state keys to persist to a JSON file (beyond latest telemetry).
# Set to None to disable custom state file.
CUSTOM_STATE_FILE = None   # e.g., 'latest_custom_state.json'

# ==================== Simulation ====================
def build_simulated_packet(tick):
    """Generate a fake telemetry line for simulation mode. Return None to disable."""
    t = tick * 0.1
    gx = 0.8 * math.sin(t / 8.0)
    gy = 1.2 * math.cos(t / 10.0)
    gz = 8.0 * math.sin(t / 12.0)
    gxyz = math.sqrt(gx**2 + gy**2 + gz**2)
    roll = 2.0 * math.sin(t / 15.0)
    pitch = 1.5 * math.cos(t / 18.0)
    yaw = 45.0 * math.sin(t / 25.0)
    ms = tick * 100
    return (
        f"TELG,ms={ms},gx={gx:.3f},gy={gy:.3f},gz={gz:.3f},"
        f"gcx={gx*0.9:.3f},gcy={gy*0.9:.3f},gcz={gz*0.9:.3f},gxyz={gxyz:.3f},"
        f"gbx=0.01,gby=-0.02,gbz=0.03,"
        f"bias_ok=1,bias_cal=0,bias_n=20000,bias_t=20000,bias_flash=1,"
        f"roll={roll:.2f},pitch={pitch:.2f},yaw={yaw:.2f},"
        f"q0=1.0,q1=0.0,q2=0.0,q3=0.0,"
        f"ax=0.01,ay=-0.02,az=1.00,anorm=1.00,"
        f"att_upd={ms}"
    )

# ==================== Status Banner ====================
def status_banner_logic(parsed):
    """
    Optional: return (state_text, health_text) based on parsed telemetry.
    Return None to use default logic.
    """
    return None
