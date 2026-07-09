import React, { createContext, useContext, useEffect, useState, useCallback } from 'react';
import { useGetMe, useAuthTelegramWebapp, setAuthTokenGetter, setBaseUrl, AuthUser } from '@workspace/api-client-react';

setBaseUrl(import.meta.env.VITE_API_BASE_URL ?? null);

interface AuthContextType {
  token: string | null;
  user: AuthUser | null;
  isLoading: boolean;
  login: (token: string) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(() => {
    const saved = localStorage.getItem('bp_token');
    if (saved) {
      setAuthTokenGetter(() => saved);
    }
    return saved;
  });

  const { data: user, isLoading: isUserLoading, isError } = useGetMe({
    query: {
      enabled: !!token,
      retry: false,
    }
  });

  const authTg = useAuthTelegramWebapp();
  const [isTgLoading, setIsTgLoading] = useState(false);

  useEffect(() => {
    // Check Telegram WebApp
    const tg = (window as any).Telegram?.WebApp;
    if (tg && tg.initData && !token) {
      setIsTgLoading(true);
      tg.ready();
      tg.expand();
      
      authTg.mutate({ data: { initData: tg.initData } }, {
        onSuccess: (session) => {
          localStorage.setItem('bp_token', session.token);
          setAuthTokenGetter(() => session.token);
          setToken(session.token);
          setIsTgLoading(false);
        },
        onError: () => {
          setIsTgLoading(false);
        }
      });
    }
  }, [token]);

  useEffect(() => {
    if (isError) {
      localStorage.removeItem('bp_token');
      setAuthTokenGetter(() => null);
      setToken(null);
    }
  }, [isError]);

  const login = useCallback((newToken: string) => {
    localStorage.setItem('bp_token', newToken);
    setAuthTokenGetter(() => newToken);
    setToken(newToken);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem('bp_token');
    setAuthTokenGetter(() => null);
    setToken(null);
  }, []);

  const value = {
    token,
    user: user || null,
    isLoading: isUserLoading || isTgLoading,
    login,
    logout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
