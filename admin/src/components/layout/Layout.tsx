import React from 'react';
import { NavLink, useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { useTheme } from '../../contexts/ThemeContext';
import { LayoutGrid, Users, MessagesSquare, Bot, Activity, LogOut, Database, Sun, Moon, PanelLeftClose, PanelLeft, Search } from 'lucide-react';
import './Layout.css';

const Layout: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { serverUrl, role, accountId, userId, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const [isSidebarCollapsed, setIsSidebarCollapsed] = React.useState(false);

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const rootItems = [
    { path: '/dashboard', label: '总览', icon: <LayoutGrid size={18} /> },
    { path: '/accounts', label: '全部租户与账号', icon: <Users size={18} /> },
    { path: '/sessions', label: '全局会话监控', icon: <MessagesSquare size={18} /> },
    { path: '/system', label: '系统监控与接口', icon: <Activity size={18} /> },
  ];

  const adminItems = [
    { path: '/dashboard', label: '工作区概览', icon: <LayoutGrid size={18} /> },
    { path: '/accounts', label: '账号组成员', icon: <Users size={18} /> },
    { path: '/resources', label: '资源库管理', icon: <Database size={18} /> },
    { path: '/recall-test', label: '检索召回测试', icon: <Search size={18} /> },
    { path: '/sessions', label: '工作区会话', icon: <MessagesSquare size={18} /> },
    { path: '/bot', label: 'Bot 测试', icon: <Bot size={18} /> },
  ];

  const userItems = [
    { path: '/resources', label: '资源库管理', icon: <Database size={18} /> },
    { path: '/recall-test', label: '检索召回测试', icon: <Search size={18} /> },
    { path: '/bot', label: 'Bot 测试', icon: <Bot size={18} /> },
  ];

  const navItems = role === 'root' ? rootItems : role === 'admin' ? adminItems : userItems;
  const currentRoleLabel = role === 'root' ? '👑 Root 超级管理员' : role === 'admin' ? `租户管理员 (${accountId})` : `${userId || '普通用户'} (${accountId})`;

  const currentPageLabel = navItems.find((item) => item.path === location.pathname)?.label || '页面';

  return (
    <div className="app-container">
      <aside className={`sidebar ${isSidebarCollapsed ? 'collapsed' : ''}`}>
        <div className="sidebar-header">
          <div className="brand" style={{ overflow: 'hidden' }}>
            <div className="brand-icon">
              <svg width="18" height="18" fill="none" stroke="#fff" strokeWidth="2.5" viewBox="0 0 24 24">
                <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
              </svg>
            </div>
            {!isSidebarCollapsed && (
              <div>
                <div className="brand-title">Support</div>
                <div className="brand-sub">Admin</div>
              </div>
            )}
          </div>
        </div>
        
        <nav className="sidebar-nav">
          {navItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
              title={isSidebarCollapsed ? item.label : undefined}
            >
              <span className="nav-icon">{item.icon}</span>
              {!isSidebarCollapsed && <span className="nav-label">{item.label}</span>}
            </NavLink>
          ))}
        </nav>
        
        <div className="sidebar-footer">
          {!isSidebarCollapsed && (
            <div className="server-info">
              <strong>{currentRoleLabel}</strong>
              <span style={{ display: 'block', fontSize: '11px', opacity: 0.7, marginTop: 4, textOverflow: 'ellipsis', overflow: 'hidden' }}>{serverUrl}</span>
            </div>
          )}
          <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
            {!isSidebarCollapsed && (
              <button className="btn btn-ghost btn-sm" style={{ flex: 1 }} onClick={handleLogout}>
                <LogOut size={14} /> 退出登录
              </button>
            )}
            <button className="btn btn-ghost btn-sm" style={isSidebarCollapsed ? { width: '100%' } : { padding: '4px 8px' }} onClick={() => setIsSidebarCollapsed(!isSidebarCollapsed)} title={isSidebarCollapsed ? '展开菜单' : '收起菜单'}>
              {isSidebarCollapsed ? <PanelLeft size={16} /> : <PanelLeftClose size={16} />}
            </button>
          </div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <h1 className="page-title">{currentPageLabel}</h1>
          <button className="btn btn-ghost theme-toggle-btn" onClick={toggleTheme} title="切换主题">
            {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </header>
        <div className={`content-container${location.pathname === '/resources' ? ' content-container--flush' : ''}`}>
          {children}
        </div>
      </main>
    </div>
  );
};

export default Layout;
