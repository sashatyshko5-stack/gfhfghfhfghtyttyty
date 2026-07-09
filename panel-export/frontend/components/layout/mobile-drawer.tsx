import React from 'react';
import { Chat, AuthUser } from '@workspace/api-client-react';
import { Drawer, DrawerContent, DrawerHeader, DrawerTitle } from '@/components/ui/drawer';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { Bot, Settings, LogOut } from '@/components/icons';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/contexts/auth-context';
import { cn } from '@/lib/utils';

interface MobileDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  chats: Chat[];
  selectedChatId: string | null;
  onSelectChat: (id: string) => void;
  view: "settings" | "ai";
  onSelectView: (view: "settings" | "ai") => void;
  user: AuthUser;
}

export function MobileDrawer({ open, onOpenChange, chats, selectedChatId, onSelectChat, view, onSelectView, user }: MobileDrawerProps) {
  const { logout } = useAuth();

  return (
    <Drawer open={open} onOpenChange={onOpenChange}>
      <DrawerContent className="max-h-[85dvh]">
        <DrawerHeader className="text-left border-b border-border pb-4">
          <DrawerTitle className="text-lg font-bold">Выберите чат</DrawerTitle>
        </DrawerHeader>
        <ScrollArea className="flex-1 p-4 overflow-auto">
          <div className="space-y-4">
            {chats.map((chat) => {
              const isSelected = chat.id === selectedChatId;
              return (
                <div key={chat.id} className="space-y-2">
                  <button
                    onClick={() => onSelectChat(chat.id)}
                    className={cn(
                      "w-full flex items-center gap-3 px-3 py-3 rounded-lg border transition-colors text-left",
                      isSelected 
                        ? "bg-primary/5 border-primary shadow-sm" 
                        : "bg-card border-border hover:bg-accent"
                    )}
                  >
                    <Avatar className="h-10 w-10 border border-border shrink-0">
                      {chat.photoUrl ? (
                        <AvatarImage src={chat.photoUrl} alt={chat.title} />
                      ) : null}
                      <AvatarFallback className="bg-primary/10 text-primary">
                        {chat.title.substring(0, 2).toUpperCase()}
                      </AvatarFallback>
                    </Avatar>
                    <div className="flex flex-col truncate">
                      <span className="font-semibold text-base truncate text-foreground">{chat.title}</span>
                      <span className="text-xs text-muted-foreground">{chat.memberCount} участников</span>
                    </div>
                  </button>

                  {isSelected && (
                    <div className="flex gap-2 pl-4 pr-1">
                      <Button
                        variant={view === "settings" ? "secondary" : "ghost"}
                        size="sm"
                        className={cn("flex-1 justify-start gap-2", view === "settings" ? "bg-primary/10 text-primary hover:bg-primary/20" : "")}
                        onClick={() => onSelectView("settings")}
                      >
                        <Settings className="h-4 w-4" />
                        Настройки
                      </Button>
                      <Button
                        variant={view === "ai" ? "secondary" : "ghost"}
                        size="sm"
                        className={cn("flex-1 justify-start gap-2", view === "ai" ? "bg-primary/10 text-primary hover:bg-primary/20" : "")}
                        onClick={() => onSelectView("ai")}
                      >
                        <Bot className="h-4 w-4" />
                        ИИ
                      </Button>
                    </div>
                  )}
                </div>
              );
            })}
            
            {chats.length === 0 && (
              <div className="text-center text-muted-foreground py-8">
                Нет доступных чатов
              </div>
            )}
          </div>
        </ScrollArea>
        <div className="p-4 border-t border-border mt-auto shrink-0 bg-muted/20">
          <div className="flex items-center gap-3">
            <Avatar className="h-10 w-10 border border-border">
              {user.photoUrl ? (
                <AvatarImage src={user.photoUrl} alt={user.firstName} />
              ) : null}
              <AvatarFallback>{user.firstName.charAt(0)}</AvatarFallback>
            </Avatar>
            <div className="flex flex-col truncate flex-1">
              <span className="text-sm font-medium text-foreground">{user.firstName} {user.lastName}</span>
              <span className="text-xs text-muted-foreground">@{user.username || "user"}</span>
            </div>
            <Button variant="ghost" size="icon" onClick={logout} className="text-muted-foreground hover:text-destructive shrink-0">
              <LogOut className="h-5 w-5" />
            </Button>
          </div>
        </div>
      </DrawerContent>
    </Drawer>
  );
}
