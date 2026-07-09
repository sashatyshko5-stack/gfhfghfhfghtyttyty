import React, { useEffect, useState } from 'react';
import { useAuth } from '@/contexts/auth-context';
import { Button } from '@/components/ui/button';
import { useDevLogin, useAuthTelegramWidget } from '@workspace/api-client-react';
import { ShieldCheck, Loader2 } from '@/components/icons';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';

export function LoginScreen() {
  const { login, isLoading } = useAuth();
  const [error, setError] = useState<string | null>(null);
  
  const devLogin = useDevLogin({
    mutation: {
      onSuccess: (data) => {
        login(data.token);
      },
      onError: (err) => {
        setError(err.error || 'Failed to login via Demo');
      }
    }
  });

  const tgWidgetLogin = useAuthTelegramWidget({
    mutation: {
      onSuccess: (data) => {
        login(data.token);
      },
      onError: (err) => {
        setError(err.error || 'Failed to authenticate via Telegram');
      }
    }
  });

  useEffect(() => {
    // Setup global callback for telegram widget
    (window as any).onTelegramAuth = (user: any) => {
      tgWidgetLogin.mutate({ data: user });
    };

    // Inject the script
    const script = document.createElement('script');
    script.src = 'https://telegram.org/js/telegram-widget.js?22';
    script.setAttribute('data-telegram-login', 'defende125_bot');
    script.setAttribute('data-size', 'large');
    script.setAttribute('data-onauth', 'onTelegramAuth(user)');
    script.setAttribute('data-request-access', 'write');
    
    const container = document.getElementById('telegram-login-widget-container');
    if (container) {
      container.appendChild(script);
    }

    return () => {
      delete (window as any).onTelegramAuth;
      if (container && script.parentNode) {
        container.removeChild(script);
      }
    };
  }, []);

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md border-border shadow-lg">
        <CardHeader className="space-y-2 text-center pb-8">
          <div className="mx-auto w-16 h-16 bg-primary/10 rounded-full flex items-center justify-center mb-4">
            <ShieldCheck className="h-8 w-8 text-primary" />
          </div>
          <CardTitle className="text-2xl font-bold tracking-tight">Панель управления</CardTitle>
          <CardDescription>
            @defende125_bot
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {error && (
            <Alert variant="destructive">
              <AlertTitle>Ошибка входа</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <div className="flex flex-col items-center gap-4">
            <div id="telegram-login-widget-container" className="min-h-[40px] flex items-center justify-center" />
            
            <div className="relative w-full">
              <div className="absolute inset-0 flex items-center">
                <span className="w-full border-t" />
              </div>
              <div className="relative flex justify-center text-xs uppercase">
                <span className="bg-card px-2 text-muted-foreground">Или</span>
              </div>
            </div>

            <Button 
              variant="outline" 
              className="w-full"
              onClick={() => devLogin.mutate()}
              disabled={devLogin.isPending || tgWidgetLogin.isPending}
            >
              {devLogin.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Демо-вход (превью)
            </Button>
            <p className="text-xs text-center text-muted-foreground mt-2">
              Используйте этот вариант, если официальный виджет недоступен.
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
