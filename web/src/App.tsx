import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { useState } from 'react';
import { getPassword, setPassword } from './lib/api';
import { useApi } from './lib/useApi';
import { api } from './lib/api';
import Dashboard from './pages/Dashboard';
import Candidates from './pages/Candidates';
import ItemDetail from './pages/ItemDetail';
import Alerts from './pages/Alerts';
import Cases from './pages/Cases';

const NAV = [
  { to: '/', label: '总览', ico: '◧', end: true },
  { to: '/candidates', label: '候选池', ico: '◎' },
  { to: '/alerts', label: '异动提醒', ico: '⚑' },
  { to: '/cases', label: '箱子稀有度', ico: '▤' },
];

export default function App() {
  const [pw, setPw] = useState(getPassword());
  const { data: health } = useApi(() => api.health(), []);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          Skin Alpha
          <small>CS2 饰品量化看板</small>
        </div>
        <nav className="nav">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? 'active' : '')}>
              <span className="ico">{n.ico}</span>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          <div>
            数据源：<span className="mono">{String(health?.db ?? '—')}</span>
            {' · '}
            <span className="mono">{String(health?.cache ?? '—')}</span>
          </div>
          <div style={{ marginTop: 10 }}>访问口令（如已开启）</div>
          <input
            type="password"
            value={pw}
            placeholder="留空表示未开启"
            onChange={(e) => {
              setPw(e.target.value);
              setPassword(e.target.value);
            }}
          />
        </div>
      </aside>

      <main className="main">
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
