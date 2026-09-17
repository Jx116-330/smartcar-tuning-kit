// 桥 API 与 control_schema.json 的类型(全部从数据驱动,无领域硬编码)

export interface LinkState {
  running: boolean;
  mode: string;
  connections: number;
  hb_age_ms: number | null;
}

export interface BridgeState {
  version: string;
  schema_hash: string | null;
  uptime_s: number;
  sse_clients: number;
}

export interface Snapshot {
  link: LinkState;
  latest: Record<string, unknown>;
  custom_state: Record<string, Record<string, unknown>>;
  packets: Record<string, Record<string, unknown>>;
  cmd_result: Record<string, unknown>;
  bridge: BridgeState;
  _ts: number;
}

// ---- control_schema.json ----
export interface SchemaField {
  id: string;
  c_id?: string;
  type?: string;          // float32 / int / bool / enum...
  min?: number;
  max?: number;
  step?: number;
  precision?: number;
  default?: unknown;
  unit?: string;
  enum?: string;          // enums 表的键
}

export interface SchemaOwner {
  id: string;
  field_set: string;
  page?: number;
  payload_words?: number;
}

// protocol.commands 条目: [名字, 模板, 是否变更类]
export type CommandTemplate = [string, string, boolean];

export interface ControlSchema {
  schema_hash?: string;
  schema_version?: number;
  fields: SchemaField[];
  enums: Record<string, Record<string, string>>;
  field_sets: Record<string, string[]>;
  owners: SchemaOwner[];
  cross_field_rules: { owner: string; rule: string; error: string }[];
  algorithms?: { fields: { id: string; bit?: number; default?: boolean }[] };
  protocol?: {
    request_prefix?: string | null;
    ack_prefix?: string;
    error_prefix?: string;
    commands?: CommandTemplate[];
    heartbeat_response?: unknown;
  };
  telemetry?: Record<string, [string, string][]>;
  actions?: ActionDecl[];
}

/** 安全动作声明(schema 顶层 actions 键;前端 SafetyActions 消费,通用核不读)。
 *  command 引用 protocol.commands 的模板名;模板缺失或含不可填占位即不渲染。 */
export interface ActionDecl {
  id: string;
  label?: string;
  command: string;
  danger?: boolean;
  hotkeys?: string[];                       // 如 ["Space","Escape"]
  retry?: { count?: number; interval_ms?: number };
  /** 悬浮/武装态激活条件:对 SSE 最新遥测求值;unit 位是枚举名时先映射再比 */
  active_when?: { stream: string; field: string; in: (string | number)[] };
}

// ---- /config (桥 UI 配置) ----
export interface PlotChannel { key: string; color: string; visible: boolean }
export interface QuickCommand { label: string; command: string }
export interface HttpConfig {
  app_title: string;
  plot_channels: PlotChannel[];
  quick_commands: QuickCommand[];
}

// ---- /batch ----
export interface BatchItemResult {
  cmd: string;
  status: 'sent' | 'ack' | 'err' | 'timeout' | 'skipped';
  elapsed_ms?: number;
  data?: Record<string, unknown>;
}
export interface BatchResponse {
  results: BatchItemResult[];
  ok: boolean;
}

// ---- SSE ----
export interface TelemetryFrame {
  type: string;
  fields: Record<string, unknown>;
  ts: number;
}
export interface ConsoleEntry {
  kind: 'cmd' | 'response' | 'ui';
  text: string;
  ts: number;
}

// ---- /proposal (P2 建议-确认流) ----
export interface Proposal {
  id: number;
  ts: number;
  commands: string[];
  rationale: string;
  expected: string;
  status: 'pending' | 'applied' | 'rejected';
  decided_ts?: number;
  results?: BatchItemResult[];
  ok?: boolean;
}
