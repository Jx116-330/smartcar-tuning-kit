// P2 建议-确认流:agent 参数提案确认条(设计文档 §3.3)。
// diff 解析全部走 schema 模板反匹配——桥只当字符串,前端用 schema 还原
// "哪条命令族/哪个字段/新值",旧值取 schema default(活值不可得时的底线)。
import { useState } from 'react';
import { decideProposal } from '../api';
import { store, useStore } from '../store';
import type { ControlSchema, Proposal } from '../types';

interface ParsedSet {
  template: string;
  field?: string;
  value?: string;
}

/** 把 schema 的 SET 模板转正则反解析命令;解析不出返回 null(原样显示)。 */
function parseSetCommand(cmd: string, schema: ControlSchema): ParsedSet | null {
  const tpls = schema.protocol?.commands ?? [];
  for (const [, tpl, mut] of tpls) {
    if (!mut) continue;
    try {
      // 模板 -> 正则:字面量转义,先消化通用占位(owner 交替/rev/其他单 token),
      // 最后才插入命名捕获组(否则收尾的通用替换会吃掉 "(?<field>...)" 里的
      // "<field>")。替换串用函数形式:避免 "$<" 被当成命名组引用。
      let re = tpl
        .replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
        .replace(/\\?<[^>|]*(?:\|[^>]*\|)[^>]*>/, () => '\\S+')  // owner 交替 <A|B|..>
        .replace(/rev=\\?<[^>]+>/, () => 'rev=\\S+')
        .replace(/\\?<(?!(field|value)>)[^>]+>/g, () => '\\S+')  // 其余单 token 占位
        .replace(/\\?<field>/, () => '(?<field>\\S+)')
        .replace(/\\?<value>/, () => '(?<value>\\S+)');
      const m = cmd.match(new RegExp(`^${re}$`));
      if (m) {
        return { template: tpl, field: m.groups?.field, value: m.groups?.value };
      }
    } catch {
      continue;   // 异常模板跳过,绝不让解析拖垮渲染
    }
  }
  return null;
}

function ProposalCard({
  proposal, schema,
}: {
  proposal: Proposal; schema: ControlSchema | null;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const pending = proposal.status === 'pending';

  const decide = async (d: 'apply' | 'reject') => {
    setBusy(true);
    setErr(null);
    try {
      await decideProposal(proposal.id, d);
      store.log('ui', `[提案#${proposal.id}] 已${d === 'apply' ? '应用' : '拒绝'}`);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`proposal-card ${pending ? '' : 'decided'}`}>
      <div className="proposal-head">
        <span className="proposal-title">agent 参数提案 #{proposal.id}</span>
        <span className={`proposal-status ${proposal.status}`}>
          {proposal.status === 'pending' ? '待确认'
            : proposal.status === 'applied'
              ? `已应用${proposal.ok === false ? '(有未ACK)' : ''}` : '已拒绝'}
        </span>
      </div>
      {proposal.rationale && (
        <div className="proposal-rationale">理由: {proposal.rationale}</div>
      )}
      {proposal.expected && (
        <div className="proposal-rationale">预期: {proposal.expected}</div>
      )}
      <div className="proposal-cmds">
        {proposal.commands.map((c, i) => {
          const parsed = schema ? parseSetCommand(c, schema) : null;
          const fld = parsed?.field
            ? schema?.fields.find((f) => f.id === parsed.field)
            : undefined;
          return (
            <div key={i} className="proposal-cmd">
              {parsed?.field
                ? <span className="proposal-diff">
                    <b>{parsed.field}</b>
                    {fld?.default !== undefined && ` (${String(fld.default)})`}
                    {' → '}<b className="proposal-newval">{parsed.value}</b>
                    {fld?.unit ? ` ${fld.unit}` : ''}
                  </span>
                : <code>{c}</code>}
            </div>
          );
        })}
      </div>
      {err && <div className="proposal-err">{err}</div>}
      {pending && (
        <div className="proposal-actions">
          <button disabled={busy} className="proposal-apply"
                  onClick={() => decide('apply')}>
            {busy ? '执行中…' : '应用(ACK 验证)'}
          </button>
          <button disabled={busy} className="proposal-reject"
                  onClick={() => decide('reject')}>
            拒绝
          </button>
        </div>
      )}
    </div>
  );
}

export default function ProposalBanner({ schema }: { schema: ControlSchema | null }) {
  const { proposals } = useStore();
  // 只展示最近 5 条;待确认的排最前
  const shown = [...proposals]
    .sort((a, b) => (a.status === 'pending' ? 0 : 1) - (b.status === 'pending' ? 0 : 1)
      || b.id - a.id)
    .slice(0, 5);
  if (!shown.length) return null;
  return (
    <div className="proposal-banner">
      {shown.map((p) => <ProposalCard key={p.id} proposal={p} schema={schema} />)}
    </div>
  );
}
