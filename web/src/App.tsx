import { useState } from 'react';
import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { api, getPassword, setPassword } from './lib/api';
import { useApi } from './lib/useApi';
import { ProgressStrip } from './components/ProgressPanel';
import Dashboard from './pages/Dashboard';
import Candidates from './pages/Candidates';
import ItemDetail from './pages/ItemDetail';
import Alerts from './pages/Alerts';
import Cases from './pages/Cases';

/**
 * 视觉契约： family = ark（黑/白/青工业信息系统）， depth = moderate
 * family 与 depth 是两个正交轴，通过根节点属性暴露，
 * 便于以后单独切换主题或加深实现层级而不动组件语义。
 */
const NAV = [
  { to: '/', label: 'Overview', zh: '总览', ico: '◧', end: true },
  { to: '/candidates', label: 'Candidates', zh: '候选池', ico: '◎' },
  { to: '/alerts', label: 'Alerts', zh: '异动', ico: '⚑' },
  { to: '/cases', label: 'Cases', zh: '箱子', ico: '▤' },
];

export default function App() {
  const [menuOpen, setMenuOpen] = useState(false);
  const { data: health } = useApi(() => api.health(), []);
  const dbUp = String(health?.db ?? '') === 'up';

  return (
    <div className="ark-shell" data-ark-theme="ark" data-ark-depth="moderate">
      <header className="ark-topbar">
        <div className="ark-brand">
          <span className="ark-brandMark" aria-hidden="true" />
          <span>
            <strong>Skin Alpha</strong>
            <small>CS2 QUANT TERMINAL</small>
          </span>
        </div>
        <div className="ark-topRight">
          <ProgressStrip />
          <span className={`ark-online ${dbUp ? '' : 'is-down'}`}>
            <i aria-hidden="true" />
            {dbUp ? 'DB ONLINE' : 'DB OFFLINE'}
          </span>
          <button
            className="ark-menu"
            type="button"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
          >
            Menu
          </button>
        </div>
      </header>

      <nav className="ark-rail" data-open={menuOpen} aria-label="主导航">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) => (isActive ? 'is-active' : '')}
            onClick={() => setMenuOpen(false)}
            title={n.zh}
          >
            <span className="ark-railIcon" aria-hidden="true">{n.ico}</span>
            <span className="ark-railLabel">{n.label}</span>
          </NavLink>
        ))}
        <button
          type="button"
          onClick={() => {
            const v = window.prompt('访问口令（留空表示未开启）', getPassword());
            if (v !== null) {
              setPassword(v);
              window.location.reload();
            }
          }}
        >
          <span className="ark-railIcon" aria-hidden="true">⌘</span>
          <span className="ark-railLabel">Access</span>
        </button>
        <div className="ark-railFoot">v1.0</div>
      </nav>

      <main className="ark-main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/candidates" element={<Candidates />} />
          <Route path="/item/:id" element={<ItemDetail />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/cases" element={<Cases />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
