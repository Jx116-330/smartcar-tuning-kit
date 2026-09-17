// 调参面板(星空版):控件仍全部从 control_schema.json 生成,但默认只显示
// "收藏"的常用参数(☆ pin,按 schema_hash 持久化 localStorage);参数库分组
// 折叠 + 搜索过滤。领域知识零硬编码。
// 双值显示:当前值(设备确认值,只读)与准备设置的值(编辑框)分离——确认值
// 只来自 SET 后回读 / 分组⟳回读 / 遥测回显,绝不用 schema 默认值冒充;
// 脏=编辑值≠确认值(琥珀条+SET 高亮),点当前值撤销编辑。
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ControlSchema, SchemaField } from '../types';
import { BatchError, postBatch } from '../api';
import { store } from '../store';
import {
  applyRequestPrefix, buildCommand, buildGetCommand, evalRule,
  extractFieldValue, extractRev, planForFieldSet,
} from '../planner';

interface Props { schema: ControlSchema }

/** 行共享上下文:确认值表 + 上报回调 + 遥测回显探测(面板级聚合下发) */
interface RowCtx {
  confirmed: Record<string, unknown>;
  onConfirmed: (id: string, v: unknown) => void;
  hasEcho: (f: SchemaField) => boolean;
}

function pinsKey(schema: ControlSchema) {
  return `tuning.pins.${(schema.schema_hash ?? 'nohash').slice(0, 12)}`;
}

function loadPins(schema: ControlSchema): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(pinsKey(schema)) ?? '[]'));
  } catch { return new Set(); }
}

/** 编辑值与确认值是否相等(数值按数值比,其余按字符串比) */
function valuesEqual(a: string, b: unknown): boolean {
  if (b === undefined || b === null) return false;
  const na = Number(a);
  const nb = Number(b);
  if (a.trim() !== '' && Number.isFinite(na) && Number.isFinite(nb)) {
    return Math.abs(na - nb) < 1e-9;
  }
  return a === String(b);
}

/** 确认值 → 编辑框字符串(数值归一到 precision 内的简洁形式) */
function editOf(v: unknown, field: SchemaField): string {
  const n = Number(v);
  if (Number.isFinite(n)) {
    return field.precision !== undefined
      ? String(Number(n.toFixed(field.precision)))
      : String(n);
  }
  return String(v);
}

/** 确认值 → 只读显示(数值按 precision;enum 映射 label;bool on/off) */
function formatConfirmed(
  v: unknown, field: SchemaField,
  enumDef?: Record<string, string>, isBool?: boolean,
): string {
  if (v === undefined || v === null) return '--';
  if (enumDef) return enumDef[String(v)] ?? String(v);
  if (isBool) return Number(v) ? 'on' : 'off';
  const n = Number(v);
  if (Number.isFinite(n)) {
    return field.precision !== undefined ? n.toFixed(field.precision) : String(n);
  }
  return String(v);
}

export default function TuningPanel({ schema }: Props) {
  const [pins, setPins] = useState<Set<string>>(() => loadPins(schema));
  const [query, setQuery] = useState('');
  // 设备确认值(只读真值):SET 后回读 / 分组⟳ / 遥测回显三个来源汇总
  const [confirmed, setConfirmed] = useState<Record<string, unknown>>({});
  const reportConfirmed = useCallback((id: string, v: unknown) => {
    setConfirmed((prev) => (prev[id] === v ? prev : { ...prev, [id]: v }));
  }, []);

  // 遥测回显:被调对象若在遥测帧里携带参数当前值(帧字段名 == field id/c_id),
  // 直接作确认值来源——零命令、随流自动刷新(契约 v1 的 params 通道即此)。
  useEffect(() => store.onTelemetry((frame) => {
    const updates: Record<string, unknown> = {};
    for (const f of schema.fields) {
      const v = frame.fields[f.id]
        ?? (f.c_id ? frame.fields[f.c_id] : undefined);
      if (v !== undefined && v !== null && v !== '') updates[f.id] = v;
    }
    if (Object.keys(updates).length === 0) return;
    setConfirmed((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const [k, v] of Object.entries(updates)) {
        if (next[k] !== v) { next[k] = v; changed = true; }
      }
      return changed ? next : prev;
    });
  }), [schema]);

  // 该字段当前是否有遥测回显覆盖(决定 SET 后是否还需主动 GET 回读)
  const hasEcho = useCallback((f: SchemaField) => {
    for (const fields of Object.values(store.state.latestByType)) {
      if (f.id in fields || (f.c_id !== undefined && f.c_id in fields)) {
        return true;
      }
    }
    return false;
  }, []);

  const fieldById = useMemo(
    () => new Map(schema.fields.map((f) => [f.id, f])), [schema]);
  // field -> 所属 field_set(取第一个;diff_gain 这类跨组字段以首组为准)
  const setOfField = useMemo(() => {
    const m = new Map<string, string>();
    for (const [setName, ids] of Object.entries(schema.field_sets)) {
      for (const id of ids) if (!m.has(id)) m.set(id, setName);
    }
    return m;
  }, [schema]);

  const groups = useMemo(
    () => Object.entries(schema.field_sets)
      .map(([name, ids]) => ({
        name,
        fields: ids.map((id) => fieldById.get(id)).filter(Boolean) as SchemaField[],
      }))
      .filter((g) => g.fields.length > 0),
    [schema, fieldById]);

  const q = query.trim().toLowerCase();
  const match = (f: SchemaField) =>
    !q || f.id.toLowerCase().includes(q) || (f.c_id ?? '').toLowerCase().includes(q);

  const pinnedFields = [...pins]
    .map((id) => fieldById.get(id))
    .filter(Boolean) as SchemaField[];

  const togglePin = (id: string) => {
    const next = new Set(pins);
    if (next.has(id)) next.delete(id); else next.add(id);
    setPins(next);
    localStorage.setItem(pinsKey(schema), JSON.stringify([...next]));
  };

  const rowCtx: RowCtx = { confirmed, onConfirmed: reportConfirmed, hasEcho };

  return (
    <div>
      <div className="panel-head">
        <div className="search-wrap">
          <input
            className="panel-search"
            placeholder={`搜索 ${schema.fields.length} 个参数…`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="panel-hint">☆ 收藏常用参数到顶部,其余折叠在参数库</div>
      </div>

      <div className="pin-section-title">★ PINNED ({pinnedFields.length})</div>
      {pinnedFields.length === 0 && (
        <div className="panel-hint" style={{ padding: '0 14px 8px' }}>
          还没有收藏 —— 在下面参数库里点 ☆
        </div>
      )}
      {pinnedFields.length > 0 && (
        <div className="fieldset-group">
          {pinnedFields.filter(match).map((f) => (
            <FieldRow key={f.id} field={f} setName={setOfField.get(f.id) ?? ''}
                      owner="" schema={schema} pinned onPin={() => togglePin(f.id)}
                      ctx={rowCtx} />
          ))}
        </div>
      )}

      <div className="lib-section-title">✦ LIBRARY</div>
      {groups.map((g) => (
        <LibraryGroup key={g.name} name={g.name}
                      fields={g.fields.filter(match)} schema={schema}
                      pins={pins} onPin={togglePin} forceOpen={!!q}
                      ctx={rowCtx} />
      ))}
    </div>
  );
}

function LibraryGroup({ name, fields, schema, pins, onPin, forceOpen, ctx }: {
  name: string; fields: SchemaField[]; schema: ControlSchema;
  pins: Set<string>; onPin: (id: string) => void; forceOpen: boolean;
  ctx: RowCtx;
}) {
  const [open, setOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const plan = useMemo(() => planForFieldSet(name, schema), [name, schema]);
  const owners = plan?.ownerChoices ?? [];
  const [owner, setOwner] = useState(owners[0] ?? '');
  const show = forceOpen || open;
  if (forceOpen && fields.length === 0) return null;

  // ⟳ 回读本组:getTemplate 含 <field> 占位的逐字段 GET(一个 batch 发完),
  // 否则整组一次 GET 后逐字段提取。失败静默——确认值保持原状,不打扰界面。
  async function refreshGroup() {
    if (!plan?.getTemplate) return;
    setRefreshing(true);
    try {
      if (plan.getTemplate.includes('<field>')) {
        const pairs = fields
          .map((f) => ({
            f,
            cmd: buildGetCommand(plan, schema, {
              field: f.id, owner: owner || undefined,
            }),
          }))
          .filter((p): p is { f: SchemaField; cmd: string } => p.cmd !== null);
        if (pairs.length > 0) {
          const r = await postBatch(
            pairs.map((p) => ({ cmd: p.cmd, expect: 'ACK' as const })));
          r.results.forEach((res, i) => {
            const v = extractFieldValue(res.data, pairs[i].f);
            if (v !== undefined) ctx.onConfirmed(pairs[i].f.id, v);
          });
        }
      } else {
        const cmd = buildGetCommand(plan, schema, { owner: owner || undefined });
        if (cmd) {
          const r = await postBatch([{ cmd, expect: 'ACK' }]);
          const data = r.results[0]?.data;
          for (const f of fields) {
            const v = extractFieldValue(data, f);
            if (v !== undefined) ctx.onConfirmed(f.id, v);
          }
        }
      }
    } catch { /* 回读失败:确认值保持原状 */ }
    finally { setRefreshing(false); }
  }

  return (
    <div className="fieldset-group">
      <h3 onClick={() => setOpen(!open)}>
        <span className="caret">{show ? '▾' : '▸'}</span> {name}
        {!plan && <span className="cnt">read-only</span>}
        {owners.length > 0 && show && (
          <select
            value={owner}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => setOwner(e.target.value)}
          >
            {owners.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        )}
        {plan?.getTemplate && show && (
          <button
            className="grp-refresh"
            title="回读本组当前值"
            disabled={refreshing}
            onClick={(e) => { e.stopPropagation(); void refreshGroup(); }}
          >{refreshing ? '…' : '⟳'}</button>
        )}
        <span className="cnt">{fields.length}</span>
      </h3>
      {show && fields.map((f) => (
        <FieldRow key={f.id} field={f} setName={name} owner={owner}
                  schema={schema} pinned={pins.has(f.id)} onPin={() => onPin(f.id)}
                  ctx={ctx} />
      ))}
    </div>
  );
}

function FieldRow({ field, setName, owner, schema, pinned, onPin, ctx }: {
  field: SchemaField; setName: string; owner: string;
  schema: ControlSchema; pinned: boolean; onPin: () => void;
  ctx: RowCtx;
}) {
  const enumDef = schema.enums?.[field.id];
  const isBool = field.type === 'bool'
    || (schema.algorithms?.fields ?? []).some((a) => a.id === field.id);
  const confirmed = ctx.confirmed[field.id];
  const [value, setValue] = useState<string>(
    field.default !== undefined ? String(field.default) : '');
  const [edited, setEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const rules = (schema.cross_field_rules ?? [])
    .filter((r) => r.owner === setName && r.rule.includes(field.id));

  const warnings = rules
    .map((r) => {
      const values: Record<string, number> = {};
      const n = Number(value);
      if (value !== '' && Number.isFinite(n)) values[field.id] = n;
      return evalRule(r.rule, values) === false ? r.rule : null;
    })
    .filter(Boolean) as string[];

  const planReady = !!setName && !!planForFieldSet(setName, schema);

  // 脏=编辑值≠确认值;确认值未知时不标脏、SET 恒可用
  const dirty = confirmed !== undefined && !valuesEqual(value, confirmed);

  // 干净时跟随:确认值更新且用户未在编辑 → 编辑框回填确认值;
  // 用户把值改回与确认值一致 → 自动脱脏(恢复跟随)
  useEffect(() => {
    if (confirmed === undefined) return;
    if (!edited) setValue(editOf(confirmed, field));
    else if (valuesEqual(value, confirmed)) setEdited(false);
    // value/field 不列入依赖:只在确认值变化时做一次仲裁
  }, [confirmed]); // eslint-disable-line react-hooks/exhaustive-deps

  // 成功反馈 2.5s 自动消隐;失败驻留到下次编辑/下发
  useEffect(() => {
    if (status?.kind !== 'ok') return;
    const t = setTimeout(() => setStatus(null), 2500);
    return () => clearTimeout(t);
  }, [status]);

  const onEdit = (v: string) => {
    setValue(v);
    setEdited(confirmed !== undefined ? !valuesEqual(v, confirmed) : true);
    if (status?.kind === 'err') setStatus(null);
  };

  // 点当前值 = 撤销编辑,回到确认值
  const revert = () => {
    if (confirmed === undefined) return;
    setValue(editOf(confirmed, field));
    setEdited(false);
    setStatus(null);
  };

  async function doSet() {
    const plan = planForFieldSet(setName, schema);
    if (!plan) return;
    setBusy(true);
    setStatus(null);
    try {
      let rev: number | undefined;
      if (plan.revToken && plan.getTemplate) {
        const getCmd = buildGetCommand(plan, schema, { owner: owner || undefined });
        if (getCmd) {
          const r = await postBatch([{ cmd: getCmd, expect: 'ACK' }]);
          rev = extractRev(r.results[0]?.data, plan.revToken);
        }
      }
      const setCmd = buildCommand(plan.setTemplate, {
        field: field.id, value, owner: owner || undefined, rev,
      });
      if (!setCmd) {
        setStatus({ kind: 'err', text: '✗ 模板填充失败' });
        store.log('ui', `✗ ${field.id}: 模板填充失败`);
        return;
      }
      const r = await postBatch(
        [{ cmd: applyRequestPrefix(setCmd, schema), expect: 'ACK' }]);
      const res = r.results[0];
      if (res?.status === 'ack') {
        // 回读:遥测回显已覆盖的字段等流自动刷新即可,否则主动 GET 一次取真值
        let got: unknown;
        if (plan.getTemplate && !ctx.hasEcho(field)) {
          const getCmd = buildGetCommand(plan, schema, {
            field: field.id, owner: owner || undefined,
          });
          if (getCmd) {
            try {
              const rg = await postBatch([{ cmd: getCmd, expect: 'ACK' }]);
              const gres = rg.results[0];
              if (gres?.status === 'ack') {
                got = extractFieldValue(gres.data, field);
              }
            } catch { /* 回读失败不掩盖 SET 成功 */ }
          }
        }
        const ms = res.elapsed_ms != null ? ` (${res.elapsed_ms}ms)` : '';
        if (got !== undefined) {
          ctx.onConfirmed(field.id, got);
          setEdited(false);
          setStatus({
            kind: 'ok',
            text: `✓ ${formatConfirmed(got, field, enumDef, isBool)}`,
          });
          store.log('ui', `${field.id} -> ack${ms},回读=${String(got)}`);
        } else {
          setStatus({
            kind: 'ok',
            text: ctx.hasEcho(field) ? '✓ ack' : '✓ ack(未回读)',
          });
          store.log('ui', `${field.id} -> ack${ms}`);
        }
      } else {
        const msg = res?.status ?? '??';
        setStatus({ kind: 'err', text: `✗ ${msg}` });
        store.log('ui', `✗ ${field.id} -> ${msg}`);
      }
    } catch (e) {
      const be = e as BatchError;
      const msg = be.violations?.length
        ? `⛔ ${be.violations.map((v) => v.detail ?? v.rule ?? '?').join('; ')}`
        : `✗ ${be.message}`;
      setStatus({ kind: 'err', text: msg });
      store.log('ui', `✗ ${field.id}: ${msg}`);
    } finally {
      setBusy(false);
    }
  }

  const confirmedText = formatConfirmed(confirmed, field, enumDef, isBool);

  return (
    <div className={`field-row${dirty ? ' dirty' : ''}`}>
      <button className={`pin ${pinned ? 'on' : ''}`} onClick={onPin}
              title={pinned ? '取消收藏' : '收藏到 PINNED'}>
        {pinned ? '★' : '☆'}
      </button>
      <label title={field.c_id ?? field.id}>
        {field.id}
        {field.unit && <span className="unit">{field.unit}</span>}
      </label>
      <span
        className={`cur${confirmed === undefined ? ' unknown' : ''}`}
        title={confirmed === undefined
          ? '当前值未回读(SET 后自动回读,或点组标题 ⟳)'
          : dirty
            ? `当前值 ${confirmedText};点击撤销编辑`
            : `当前值 ${confirmedText}(已同步)`}
        onClick={dirty && confirmed !== undefined ? revert : undefined}
      >{confirmedText}</span>
      {enumDef ? (
        <select value={value} onChange={(e) => onEdit(e.target.value)}>
          {Object.entries(enumDef).map(([k, label]) => (
            <option key={k} value={k}>{label}</option>
          ))}
        </select>
      ) : isBool ? (
        <select value={value} onChange={(e) => onEdit(e.target.value)}>
          <option value="1">on</option>
          <option value="0">off</option>
        </select>
      ) : (
        <input
          type="number" value={value}
          min={field.min} max={field.max} step={field.step ?? 'any'}
          onChange={(e) => onEdit(e.target.value)}
        />
      )}
      <button className="btn"
              disabled={!planReady || busy || value === ''
                        || (confirmed !== undefined && !dirty)}
              title={confirmed !== undefined && !dirty
                ? '与当前值一致,无需下发' : undefined}
              onClick={doSet}>
        {busy ? '…' : 'SET'}
      </button>
      {status && <div className={`row-status ${status.kind}`}>{status.text}</div>}
      {warnings.map((w) => <div key={w} className="warn">⚠ rule: {w}</div>)}
    </div>
  );
}
