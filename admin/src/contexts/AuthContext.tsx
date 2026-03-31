import React, { createContext, useContext, useState, useEffect } from 'react';

interface AuthContextType {
  serverUrl: string;
  apiKey: string;
  role: string | null;
  accountId: string | null;
  setAuth: (url: string, key: string, role: string, accountId: string, remember?: boolean) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [serverUrl, setServerUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [role, setRole] = useState<string | null>(null);
  const [accountId, setAccountId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Check sessionStorage first (high priority), then localStorage (Remember Me)
    const url = sessionStorage.getItem('ov_admin_url') || localStorage.getItem('ov_admin_url');
    const key = sessionStorage.getItem('ov_admin_key') || localStorage.getItem('ov_admin_key');
    const savedRole = sessionStorage.getItem('ov_admin_role') || localStorage.getItem('ov_admin_role');
    const savedAccountId = sessionStorage.getItem('ov_admin_account') || localStorage.getItem('ov_admin_account');
    if (url) {
      setServerUrl(url);
      setApiKey(key || '');
      setRole(savedRole);
      setAccountId(savedAccountId);
    }
    setLoading(false);
  }, []);

  const setAuth = (url: string, key: string, newRole: string, newAccountId: string, remember: boolean = false) => {
    // Basic formatting
    const formattedUrl = url.trim().replace(/\/$/, '');
    setServerUrl(formattedUrl);
    setApiKey(key);
    setRole(newRole);
    setAccountId(newAccountId);
    
    // Default to sessionStorage (secure/ephemeral), use localStorage only if checked
    const storage = remember ? localStorage : sessionStorage;
    storage.setItem('ov_admin_url', formattedUrl);
    storage.setItem('ov_admin_key', key);
    storage.setItem('ov_admin_role', newRole);
    if (newAccountId) storage.setItem('ov_admin_account', newAccountId);

    // If remember is false, ensure we clear any old localStorage
    if (!remember) {
      localStorage.removeItem('ov_admin_url');
      localStorage.removeItem('ov_admin_key');
      localStorage.removeItem('ov_admin_role');
      localStorage.removeItem('ov_admin_account');
    } else {
      sessionStorage.removeItem('ov_admin_url');
      sessionStorage.removeItem('ov_admin_key');
      sessionStorage.removeItem('ov_admin_role');
      sessionStorage.removeItem('ov_admin_account');
    }
  };

  const logout = () => {
    setServerUrl('');
    setApiKey('');
    setRole(null);
    setAccountId(null);
    localStorage.removeItem('ov_admin_url');
    localStorage.removeItem('ov_admin_key');
    localStorage.removeItem('ov_admin_role');
    localStorage.removeItem('ov_admin_account');
    sessionStorage.removeItem('ov_admin_url');
    sessionStorage.removeItem('ov_admin_key');
    sessionStorage.removeItem('ov_admin_role');
    sessionStorage.removeItem('ov_admin_account');
  };

  if (loading) return null;

  return (
    <AuthContext.Provider value={{ serverUrl, apiKey, role, accountId, setAuth, logout }}>
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

