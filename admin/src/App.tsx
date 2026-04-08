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

const ProtectedRoute = ({ children }: { children: React.ReactNode }) => {
  const { serverUrl } = useAuth();
  if (!serverUrl) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
};

const AppRoutes = () => {
  const { role } = useAuth();
  const defaultPage = role === 'user' ? '/resources' : '/dashboard';

  return (
    <Routes>
      {/* Public route: no login required, credentials come from URL params */}
      <Route path="/test-bot" element={<TestBot />} />
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
  return (
    <BrowserRouter basename="/admin">
      <ThemeProvider>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </ThemeProvider>
    </BrowserRouter>
  );
};

export default App;
