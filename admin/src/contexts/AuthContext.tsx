import React, { createContext, useContext, useState, useEffect } from 'react';

interface AuthContextType {
  serverUrl: string;
  apiKey: string;
  role: string | null;
  accountId: string | null;
  userId: string | null;
  setAuth: (url: string, key: string, role: string, accountId: string, userId: string, remember?: boolean) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [serverUrl, setServerUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [role, setRole] = useState<string | null>(null);
  const [accountId, setAccountId] = useState<string | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Check sessionStorage first (high priority), then localStorage (Remember Me)
    const url = sessionStorage.getItem('ov_admin_url') || localStorage.getItem('ov_admin_url');
    const key = sessionStorage.getItem('ov_admin_key') || localStorage.getItem('ov_admin_key');
    const savedRole = sessionStorage.getItem('ov_admin_role') || localStorage.getItem('ov_admin_role');
    const savedAccountId = sessionStorage.getItem('ov_admin_account') || localStorage.getItem('ov_admin_account');
    const savedUserId = sessionStorage.getItem('ov_admin_user') || localStorage.getItem('ov_admin_user');
    if (url) {
      setServerUrl(url);
      setApiKey(key || '');
      setRole(savedRole);
      setAccountId(savedAccountId);
      setUserId(savedUserId);
    }
    setLoading(false);
  }, []);

  const setAuth = (url: string, key: string, newRole: string, newAccountId: string, newUserId: string, remember: boolean = false) => {
    // Basic formatting
    const formattedUrl = url.trim().replace(/\/$/, '');
    setServerUrl(formattedUrl);
    setApiKey(key);
    setRole(newRole);
    setAccountId(newAccountId);
    setUserId(newUserId);
    
    // Default to sessionStorage (secure/ephemeral), use localStorage only if checked
    const storage = remember ? localStorage : sessionStorage;
    storage.setItem('ov_admin_url', formattedUrl);
    storage.setItem('ov_admin_key', key);
    storage.setItem('ov_admin_role', newRole);
    if (newAccountId) storage.setItem('ov_admin_account', newAccountId);
    if (newUserId) storage.setItem('ov_admin_user', newUserId);

    // If remember is false, ensure we clear any old localStorage
    if (!remember) {
      localStorage.removeItem('ov_admin_url');
      localStorage.removeItem('ov_admin_key');
      localStorage.removeItem('ov_admin_role');
      localStorage.removeItem('ov_admin_account');
      localStorage.removeItem('ov_admin_user');
    } else {
      sessionStorage.removeItem('ov_admin_url');
      sessionStorage.removeItem('ov_admin_key');
      sessionStorage.removeItem('ov_admin_role');
      sessionStorage.removeItem('ov_admin_account');
      sessionStorage.removeItem('ov_admin_user');
    }
  };

  const logout = () => {
    setServerUrl('');
    setApiKey('');
    setRole(null);
    setAccountId(null);
    setUserId(null);
    localStorage.removeItem('ov_admin_url');
    localStorage.removeItem('ov_admin_key');
    localStorage.removeItem('ov_admin_role');
    localStorage.removeItem('ov_admin_account');
    localStorage.removeItem('ov_admin_user');
    sessionStorage.removeItem('ov_admin_url');
    sessionStorage.removeItem('ov_admin_key');
    sessionStorage.removeItem('ov_admin_role');
    sessionStorage.removeItem('ov_admin_account');
    sessionStorage.removeItem('ov_admin_user');
  };

  if (loading) return null;

  return (
    <AuthContext.Provider value={{ serverUrl, apiKey, role, accountId, userId, setAuth, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

