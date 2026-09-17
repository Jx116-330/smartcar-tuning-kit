import { useEffect, useState } from 'react';
import { api } from './api';
import type { ControlSchema, HttpConfig, Snapshot } from './types';
import Header from './components/Header';
import Starfield from './components/Starfield';
import TuningPanel from './components/TuningPanel';
import ChartsTab from './components/ChartsTab';
import TrajectoryTab from './components/TrajectoryTab';
import CompareTab from './components/CompareTab';
import Console from './components/Console';
import ProposalBanner from './components/ProposalBanner';
import SafetyActions from './components/SafetyActions';

type Tab = 'charts' | 'trajectory' | 'compare';

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [schema, setSchema] = useState<ControlSchema | null>(null);
  const [config, setConfig] = useState<HttpConfig | null>(null);
  const [tab, setTab] = useState<Tab>('charts');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.snapshot(), api.config()])
      .then(([s, c]) => { setSnapshot(s); setConfig(c); })
      .catch((e) => setError(`桥不可达: ${e.message}`));
    api.schema()
      .then(setSchema)
      .catch(() => setSchema(null));   // schema 缺失时调参面板降级隐藏
  }, []);

  return (
    <>
      <Starfield />
      <div className="app">
        <Header snapshot={snapshot} config={config} error={error}
                actions={<SafetyActions schema={schema} />} />
        <ProposalBanner schema={schema} />
        <aside className="panel">
          {schema
            ? <TuningPanel schema={schema} />
            : <div className="empty-hint" style={{ position: 'static', height: '100%' }}>
                no control_schema.json
              </div>}
        </aside>
        <main className="main">
          <div className="tabs">
            <button className={tab === 'charts' ? 'active' : ''} onClick={() => setTab('charts')}>
              实时曲线
            </button>
            <button className={tab === 'trajectory' ? 'active' : ''} onClick={() => setTab('trajectory')}>
              轨迹 XY
            </button>
            <button className={tab === 'compare' ? 'active' : ''} onClick={() => setTab('compare')}>
              趟对比分析
            </button>
          </div>
          <div className="tab-body">
            {tab === 'charts' && <ChartsTab config={config} />}
            {tab === 'trajectory' && <TrajectoryTab />}
            {tab === 'compare' && <CompareTab />}
          </div>
        </main>
        <section className="console">
          <Console config={config} />
        </section>
      </div>
    </>
  );
}
