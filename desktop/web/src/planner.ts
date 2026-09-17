// 通用命令规划器:一切知识来自 schema(protocol.commands 模板 + field_sets +
// owners)。field_set → 命令族的解析走命名约定(归一化后前缀/后缀匹配),
// 备选:模板里同时含 <field> 与 <|> 交替占位符的即 owner 族。无任何领域字面量。
import type { CommandTemplate, ControlSchema } from './types';

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, '');

export interface SetPlan {
  family: string;
  getTemplate?: string;      // 取 rev 用的 GET 模板(可能含 owner 占位)
  setTemplate: string;       // SET 模板
  isOwnerFamily: boolean;    // 模板含 <|> owner 占位
  ownerChoices: string[];    // RunData 类:owners 的 id 列表
  revToken?: string;         // 模板里 rev=<token> 的 token 名
}

function familyOf(tplName: string): string | null {
  const m = tplName.match(/^(.+?)_SET$/);
  return m ? m[1] : null;
}

/** field_set 名 → 命令族(命名约定匹配,匹配不到返回 null = 该组不可设) */
export function planForFieldSet(
  setName: string, schema: ControlSchema,
): SetPlan | null {
  const cmds = schema.protocol?.commands ?? [];
  const setTpls = cmds.filter(
    ([n, t, mut]) => mut && n.endsWith('_SET') && t.includes('<field>'),
  );
  // 1) 命名约定:family 与 field_set 名归一化后互为前缀/后缀
  const hit = setTpls.find(([n]) => {
    const fam = familyOf(n);
    if (!fam) return false;
    const a = norm(setName); const b = norm(fam);
    return a.startsWith(b) || a.endsWith(b) || b.startsWith(a) || b.endsWith(a);
  });
  // 2) 备选:含 owner 交替占位的模板(如 DATA SET <P1|P2|..> <field> <value>)
  const chosen = hit ?? setTpls.find(([, t]) => /<[^>]*\|[^>]*>/.test(t));
  if (!chosen) return null;
  const [name, setTemplate] = chosen as CommandTemplate;
  const family = familyOf(name)!;
  const getTpl = cmds.find(
    ([n, , mut]) => !mut && n === `${family}_GET`,
  );
  const owners = (schema.owners ?? [])
    .filter((o) => o.field_set === setName)
    .map((o) => o.id);
  const isOwnerFamily = /<[^>]*\|[^>]*>/.test(setTemplate);
  const revM = setTemplate.match(/rev=<([^>]+)>/);
  return {
    family,
    getTemplate: getTpl?.[1],
    setTemplate,
    isOwnerFamily,
    ownerChoices: isOwnerFamily ? owners : [],
    revToken: revM?.[1],
  };
}

/** 用字段值填模板;rev 缺失时整段 ` rev=<..>` 移除。填不完返回 null。 */
export function buildCommand(
  template: string,
  fill: { field?: string; value?: string; owner?: string; rev?: number },
): string | null {
  let cmd = template;
  if (fill.field !== undefined) cmd = cmd.replace('<field>', fill.field);
  if (fill.value !== undefined) cmd = cmd.replace('<value>', fill.value);
  if (fill.owner !== undefined) cmd = cmd.replace(/<[^>]*\|[^>]*>/, fill.owner);
  if (fill.rev !== undefined) {
    cmd = cmd.replace(/rev=<[^>]+>/, `rev=${fill.rev}`);
  } else {
    cmd = cmd.replace(/\s*rev=<[^>]+>/, '');
  }
  if (/<[^>]+>/.test(cmd)) return null;   // 还有占位符没填上
  return cmd.trim();
}

/** 从 GET 响应 data 里提某字段的当前值:归一化(小写+去非字母数字)后
 *  精确匹配 field.id,其次 field.c_id。找不到返回 undefined
 *  (调用方走"ack 未回读"降级,不阻塞不误导)。 */
export function extractFieldValue(
  data: Record<string, unknown> | undefined,
  field: { id: string; c_id?: string },
): unknown {
  if (!data) return undefined;
  const entries = Object.entries(data).filter(([k]) => !k.startsWith('_'));
  const nid = norm(field.id);
  const hit = entries.find(([k]) => norm(k) === nid);
  if (hit) return hit[1];
  if (field.c_id) {
    const ncid = norm(field.c_id);
    const hit2 = entries.find(([k]) => norm(k) === ncid);
    if (hit2) return hit2[1];
  }
  return undefined;
}

/** 构造回读 GET 命令:getTemplate 含 <field> 占位时按字段取,否则整组取
 *  (owner 占位照常填)。无 getTemplate 或填不完返回 null。 */
export function buildGetCommand(
  plan: SetPlan, schema: ControlSchema,
  fill: { field?: string; owner?: string },
): string | null {
  if (!plan.getTemplate) return null;
  const cmd = buildCommand(plan.getTemplate, {
    field: plan.getTemplate.includes('<field>') ? fill.field : undefined,
    owner: fill.owner,
  });
  return cmd ? applyRequestPrefix(cmd, schema) : null;
}

/** 从 GET 响应 data 里找 rev 值:优先 token 名精确匹配,其次包含,再次任何
 *  含 'rev' 的数值键。找不到返回 undefined(调用方去掉 rev 段)。 */
export function extractRev(
  data: Record<string, unknown> | undefined, token?: string,
): number | undefined {
  if (!data) return undefined;
  const entries = Object.entries(data)
    .filter(([k, v]) => !k.startsWith('_') && typeof v === 'number');
  const lower = token?.toLowerCase();
  if (lower) {
    const exact = entries.find(([k]) => k.toLowerCase() === lower);
    if (exact) return exact[1] as number;
    const part = entries.find(([k]) => k.toLowerCase().includes(lower));
    if (part) return part[1] as number;
  }
  const any = entries.find(([k]) => k.toLowerCase().includes('rev'));
  return any ? (any[1] as number) : undefined;
}

let ridCounter = 1;

/** schema.protocol.request_prefix 存在时(如 "REQ <rid> ")给命令加请求前缀 */
export function applyRequestPrefix(cmd: string, schema: ControlSchema): string {
  const p = schema.protocol?.request_prefix;
  if (!p) return cmd;
  return p.replace('<rid>', String(ridCounter++)) + cmd;
}

// ---- cross_field_rules:形如 "RecMinStep<=RecDistStep" 的提示性校验 ----
const RULE_RE = /^(.+?)(<=|>=|==|!=|<|>)(.+)$/;

export function evalRule(
  rule: string, values: Record<string, number>,
): boolean | null {
  const m = rule.match(RULE_RE);
  if (!m) return null;
  const [, l, op, r] = m;
  const resolve = (s: string): number | null => {
    const t = s.trim();
    if (t in values) return values[t];
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
  };
  const a = resolve(l); const b = resolve(r);
  if (a === null || b === null) return null;   // 操作数不全 = 不评
  switch (op) {
    case '<=': return a <= b;
    case '>=': return a >= b;
    case '<': return a < b;
    case '>': return a > b;
    case '==': return a === b;
    case '!=': return a !== b;
    default: return null;
  }
}
