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
                <Route path="/bot" element={<BotChat />} />
                <Route path="/system" element={<SystemInfo />} />
                <Route path="/resources" element={<Resources />} />
                <Route path="*" element={<Navigate to={defaultPage} replace />} />
              </Routes>
            </Layout>
          </ProtectedRoute>
        }
      />
    </Routes>
  );
};

const App = () => {
  return (
    <BrowserRouter basename="/admin">
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
};

export default App;
