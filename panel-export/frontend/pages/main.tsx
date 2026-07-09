import React, { useState } from 'react';
import { useListChats, useGetMe } from '@workspace/api-client-react';
import { Sidebar } from '@/components/layout/sidebar';
import { MobileDrawer } from '@/components/layout/mobile-drawer';
import { ChatSettings } from '@/pages/chat-settings';
import { ChatAi } from '@/pages/chat-ai';
import { Loader2, Menu } from '@/components/icons';
import { Button } from '@/components/ui/button';

export function MainShell() {
  const { data: chats, isLoading } = useListChats();
  const { data: me } = useGetMe();
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
  const [view, setView] = useState<"settings" | "ai">("settings");
  const [mobileOpen, setMobileOpen] = useState(false);

  const handleSelectChat = (id: string) => {
    setSelectedChatId(id);
    setView("settings");
    setMobileOpen(false);
  };

  const handleSelectView = (newView: "settings" | "ai") => {
    setView(newView);
    setMobileOpen(false);
  };

  if (isLoading || !me) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  const selectedChat = chats?.find((c) => c.id === selectedChatId);

  return (
    <div className="flex h-[100dvh] w-full overflow-hidden bg-background">
      {/* Desktop Sidebar */}
      <div className="hidden md:flex h-full w-72 flex-col border-r border-border bg-sidebar shrink-0">
        <Sidebar 
          chats={chats || []} 
          selectedChatId={selectedChatId} 
          onSelectChat={handleSelectChat}
          view={view}
          onSelectView={handleSelectView}
          user={me!}
        />
      </div>

      {/* Mobile Drawer */}
      <MobileDrawer
        open={mobileOpen}
        onOpenChange={setMobileOpen}
        chats={chats || []}
        selectedChatId={selectedChatId}
        onSelectChat={handleSelectChat}
        view={view}
        onSelectView={handleSelectView}
        user={me!}
      />

      {/* Main Content Area */}
      <main className="flex-1 flex flex-col min-w-0 h-full overflow-hidden relative">
        <div className="md:hidden flex items-center h-14 border-b border-border px-4 shrink-0 bg-card">
          <Button variant="ghost" size="icon" onClick={() => setMobileOpen(true)} className="-ml-2">
            <Menu className="h-6 w-6" />
          </Button>
          <div className="ml-2 font-semibold truncate">
            {selectedChat ? selectedChat.title : "Панель управления"}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {!selectedChatId ? (
            <div className="h-full flex flex-col items-center justify-center text-muted-foreground p-4 text-center">
              <div className="w-16 h-16 rounded-full bg-accent flex items-center justify-center mb-4">
                <Menu className="h-8 w-8 text-accent-foreground" />
              </div>
              <p className="text-lg font-medium text-foreground">Выберите чат</p>
              <p className="text-sm mt-1">Для настройки параметров защиты выберите чат из списка</p>
            </div>
          ) : view === "settings" ? (
            <ChatSettings chatId={selectedChatId} />
          ) : (
            <ChatAi chatId={selectedChatId} onBack={() => handleSelectView("settings")} />
          )}
        </div>
      </main>
    </div>
  );
}
