import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import Layout from './components/layout/Layout';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Accounts from './pages/Accounts';
import Sessions from './pages/Sessions';
import BotChat from './pages/BotChat';
import SystemInfo from './pages/SystemInfo';
import Resources from './pages/Resources';
import RecallTest from './pages/RecallTest';
import TestBot from './pages/TestBot';
import AdminMemories from './pages/AdminMemories';

const getRouterBase = () => {
  if (typeof window !== 'undefined' && window.location.pathname.startsWith('/guest')) {
    return '/guest';
  }
  return '/admin';
};

const ProtectedRoute = ({ children }: { children: React.ReactNode }) => {
  const { serverUrl } = useAuth();
  if (!serverUrl) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
};

const GuestAppRoutes = () => (
  <Routes>
    <Route path="/" element={<TestBot />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>
);

const AdminAppRoutes = () => {
  const { role } = useAuth();
  const defaultPage = role === 'user' ? '/resources' : '/dashboard';

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/*"
        element={
          <ProtectedRoute>
            <Layout>
              <Routes>
                <Route path="/" element={<Navigate to={defaultPage} replace />} />
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="/accounts" element={<Accounts />} />
                <Route path="/sessions" element={<Sessions />} />
                {role !== 'root' && <Route path="/bot" element={<BotChat />} />}
                <Route path="/system" element={<SystemInfo />} />
                {role !== 'user' && <Route path="/memories" element={<AdminMemories />} />}
                <Route path="/resources" element={<Resources />} />
                {role !== 'root' && <Route path="/recall-test" element={<RecallTest />} />}
                <Route path="*" element={<Navigate to={defaultPage} replace />} />
              </Routes>
            </Layout>
          </ProtectedRoute>
        }
      />
    </Routes>
  );
};

import { ThemeProvider } from './contexts/ThemeContext';

const App = () => {
  const routerBase = getRouterBase();
  const guestMode = routerBase === '/guest';

  return (
    <BrowserRouter basename={routerBase}>
      <ThemeProvider>
        <AuthProvider>
          {guestMode ? <GuestAppRoutes /> : <AdminAppRoutes />}
        </AuthProvider>
      </ThemeProvider>
    </BrowserRouter>
  );
};

export default App;
