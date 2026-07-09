import React, { useState } from 'react';
import { Chat, AuthUser } from '@workspace/api-client-react';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Button } from '@/components/ui/button';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { Bot, Settings, Users, LogOut, ChevronLeft, ChevronRight } from '@/components/icons';
import { useAuth } from '@/contexts/auth-context';
import { cn } from '@/lib/utils';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

interface SidebarProps {
  chats: Chat[];
  selectedChatId: string | null;
  onSelectChat: (id: string) => void;
  view: "settings" | "ai";
  onSelectView: (view: "settings" | "ai") => void;
  user: AuthUser;
}

export function Sidebar({ chats, selectedChatId, onSelectChat, view, onSelectView, user }: SidebarProps) {
  const { logout } = useAuth();
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className={cn("flex flex-col h-full bg-sidebar text-sidebar-foreground transition-all duration-300", collapsed ? "w-[60px]" : "w-full")}>
      <div className="h-14 flex items-center justify-between px-3 border-b border-sidebar-border shrink-0">
        {!collapsed && <span className="font-semibold text-sm uppercase tracking-wider text-sidebar-foreground/70">Мои Чаты</span>}
        <Button variant="ghost" size="icon" onClick={() => setCollapsed(!collapsed)} className="h-8 w-8 text-sidebar-foreground/70 hover:text-sidebar-foreground">
          {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
        </Button>
      </div>

      <ScrollArea className="flex-1 py-2">
        <div className="space-y-1 px-2">
          {chats.map((chat) => {
            const isSelected = chat.id === selectedChatId;
            return (
              <div key={chat.id} className="space-y-1">
                <Tooltip delayDuration={collapsed ? 0 : 1000}>
                  <TooltipTrigger asChild>
                    <button
                      onClick={() => onSelectChat(chat.id)}
                      className={cn(
                        "w-full flex items-center gap-3 px-2 py-2 rounded-md transition-colors text-sm font-medium",
                        isSelected 
                          ? "bg-sidebar-accent text-sidebar-accent-foreground" 
                          : "hover:bg-sidebar-accent/50 text-sidebar-foreground/80 hover:text-sidebar-foreground",
                        collapsed && "justify-center px-0"
                      )}
                    >
                      <Avatar className="h-7 w-7 border border-sidebar-border/50 shrink-0">
                        {chat.photoUrl ? (
                          <AvatarImage src={chat.photoUrl} alt={chat.title} />
                        ) : null}
                        <AvatarFallback className="bg-primary/10 text-primary text-[10px]">
                          {chat.title.substring(0, 2).toUpperCase()}
                        </AvatarFallback>
                      </Avatar>
                      {!collapsed && (
                        <div className="flex flex-col items-start truncate overflow-hidden">
                          <span className="truncate w-full text-left">{chat.title}</span>
                          <span className="text-[10px] text-sidebar-foreground/50 font-normal">{chat.memberCount} участников</span>
                        </div>
                      )}
                    </button>
                  </TooltipTrigger>
                  {collapsed && <TooltipContent side="right">{chat.title}</TooltipContent>}
                </Tooltip>

                {isSelected && !collapsed && (
                  <div className="pl-9 pr-2 space-y-1 pb-2">
                    <button
                      onClick={() => onSelectView("settings")}
                      className={cn(
                        "w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-xs font-medium transition-colors",
                        view === "settings" ? "bg-primary/10 text-primary" : "text-sidebar-foreground/70 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
                      )}
                    >
                      <Settings className="h-3.5 w-3.5" />
                      Настройки
                    </button>
                    <button
                      onClick={() => onSelectView("ai")}
                      className={cn(
                        "w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-xs font-medium transition-colors",
                        view === "ai" ? "bg-primary/10 text-primary" : "text-sidebar-foreground/70 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
                      )}
                    >
                      <Bot className="h-3.5 w-3.5" />
                      ИИ Настройки
                    </button>
                  </div>
                )}
                {isSelected && collapsed && (
                  <div className="flex flex-col gap-1 items-center pb-2">
                    <Tooltip delayDuration={0}>
                      <TooltipTrigger asChild>
                        <button
                          onClick={() => onSelectView("settings")}
                          className={cn(
                            "h-8 w-8 flex items-center justify-center rounded-md transition-colors",
                            view === "settings" ? "bg-primary/10 text-primary" : "text-sidebar-foreground/70 hover:bg-sidebar-accent/50"
                          )}
                        >
                          <Settings className="h-4 w-4" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent side="right">Настройки</TooltipContent>
                    </Tooltip>
                    <Tooltip delayDuration={0}>
                      <TooltipTrigger asChild>
                        <button
                          onClick={() => onSelectView("ai")}
                          className={cn(
                            "h-8 w-8 flex items-center justify-center rounded-md transition-colors",
                            view === "ai" ? "bg-primary/10 text-primary" : "text-sidebar-foreground/70 hover:bg-sidebar-accent/50"
                          )}
                        >
                          <Bot className="h-4 w-4" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent side="right">ИИ Настройки</TooltipContent>
                    </Tooltip>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </ScrollArea>

      <div className="p-3 border-t border-sidebar-border shrink-0">
        <div className={cn("flex items-center gap-3", collapsed ? "justify-center" : "")}>
          <Avatar className="h-9 w-9 border border-sidebar-border shrink-0">
            {user.photoUrl ? (
              <AvatarImage src={user.photoUrl} alt={user.firstName} />
            ) : null}
            <AvatarFallback className="bg-secondary text-secondary-foreground text-xs">
              {user.firstName.charAt(0)}
            </AvatarFallback>
          </Avatar>
          {!collapsed && (
            <div className="flex flex-col truncate flex-1">
              <span className="text-sm font-medium truncate">{user.firstName} {user.lastName}</span>
              <span className="text-xs text-sidebar-foreground/60 truncate">@{user.username || "user"}</span>
            </div>
          )}
          {!collapsed && (
            <Button variant="ghost" size="icon" onClick={logout} className="h-8 w-8 text-sidebar-foreground/60 hover:text-destructive shrink-0">
              <LogOut className="h-4 w-4" />
            </Button>
          )}
        </div>
        {collapsed && (
          <Button variant="ghost" size="icon" onClick={logout} className="w-full h-8 mt-2 text-sidebar-foreground/60 hover:text-destructive">
            <LogOut className="h-4 w-4" />
          </Button>
        )}
      </div>
    </div>
  );
}
