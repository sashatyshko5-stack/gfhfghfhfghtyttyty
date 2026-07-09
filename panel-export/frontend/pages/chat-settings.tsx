import React, { useEffect, useRef } from 'react';
import { 
  useGetChatSettings, 
  useUpdateAntispam, 
  useUpdateAntiNsfw, 
  useUpdateAntiRaid, 
  useGetAntiRaidStatus, 
  useLiftAntiRaidLockdown, 
  useListModerators,
  getGetChatSettingsQueryKey,
  getGetAntiRaidStatusQueryKey,
  ChatSettings as ChatSettingsType
} from '@workspace/api-client-react';
import { useQueryClient } from '@tanstack/react-query';
import { Card, CardContent, CardDescription, CardHeader, CardTitle, CardFooter } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';
import { Slider } from '@/components/ui/slider';
import { Badge } from '@/components/ui/badge';
import { Loader2, ShieldCheck, ShieldAlert, ShieldBan, Bot, Users } from '@/components/icons';
import { useToast } from '@/hooks/use-toast';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';

interface ChatSettingsProps {
  chatId: string;
}

export function ChatSettings({ chatId }: ChatSettingsProps) {
  const { data: settings, isLoading, isError } = useGetChatSettings(chatId);
  const { data: raidStatus } = useGetAntiRaidStatus(chatId, { query: { refetchInterval: 5000 } });
  const { data: moderators } = useListModerators(chatId);
  
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const updateAntispam = useUpdateAntispam();
  const updateAntiNsfw = useUpdateAntiNsfw();
  const updateAntiRaid = useUpdateAntiRaid();
  const liftRaidLockdown = useLiftAntiRaidLockdown();

  if (isLoading) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  if (isError || !settings) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground p-8 text-center">
        <ShieldAlert className="h-10 w-10 text-destructive" />
        <p className="font-medium text-foreground">Не удалось загрузить настройки</p>
        <p className="text-sm">Проверьте подключение и попробуйте обновить страницу.</p>
      </div>
    );
  }

  // Helpers for patching cache instantly
  const patchSettings = (updater: (old: ChatSettingsType) => ChatSettingsType) => {
    queryClient.setQueryData(getGetChatSettingsQueryKey(chatId), (old: ChatSettingsType | undefined) => {
      if (!old) return old;
      return updater(old);
    });
  };

  const handleAntispamChange = (data: Partial<ChatSettingsType['antispam']>) => {
    patchSettings(old => ({ ...old, antispam: { ...old.antispam, ...data } }));
    updateAntispam.mutate({ chatId, data }, {
      onSuccess: () => toast({ title: "Сохранено", description: "Настройки Антиспам обновлены" }),
      onError: () => {
        toast({ title: "Ошибка", description: "Не удалось сохранить настройки", variant: "destructive" });
        queryClient.invalidateQueries({ queryKey: getGetChatSettingsQueryKey(chatId) });
      }
    });
  };

  const handleAntiNsfwChange = (data: Partial<ChatSettingsType['antinsfw']>) => {
    patchSettings(old => ({ ...old, antinsfw: { ...old.antinsfw, ...data } }));
    updateAntiNsfw.mutate({ chatId, data }, {
      onSuccess: () => toast({ title: "Сохранено", description: "Настройки Защиты от 18+ обновлены" }),
      onError: () => {
        toast({ title: "Ошибка", description: "Не удалось сохранить настройки", variant: "destructive" });
        queryClient.invalidateQueries({ queryKey: getGetChatSettingsQueryKey(chatId) });
      }
    });
  };

  const handleAntiRaidChange = (data: Partial<ChatSettingsType['anti_raid']>) => {
    patchSettings(old => ({ ...old, anti_raid: { ...old.anti_raid, ...data } }));
    updateAntiRaid.mutate({ chatId, data }, {
      onSuccess: () => toast({ title: "Сохранено", description: "Настройки Антирейд обновлены" }),
      onError: () => {
        toast({ title: "Ошибка", description: "Не удалось сохранить настройки", variant: "destructive" });
        queryClient.invalidateQueries({ queryKey: getGetChatSettingsQueryKey(chatId) });
      }
    });
  };

  const handleLiftLockdown = () => {
    liftRaidLockdown.mutate({ chatId }, {
      onSuccess: () => {
        toast({ title: "Блокировка снята", description: "Чат снова открыт для новых участников" });
        queryClient.invalidateQueries({ queryKey: getGetAntiRaidStatusQueryKey(chatId) });
      },
      onError: () => toast({ title: "Ошибка", description: "Не удалось снять блокировку", variant: "destructive" })
    });
  };

  return (
    <div className="max-w-4xl mx-auto p-4 md:p-8 space-y-8 pb-24">
      
      {/* ----------------- АНТИРЕЙД ----------------- */}
      <Card className="border-border shadow-sm">
        <CardHeader className="border-b border-border bg-card/50 pb-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-md bg-destructive/10 flex items-center justify-center">
                <ShieldAlert className="h-5 w-5 text-destructive" />
              </div>
              <div>
                <CardTitle className="text-xl">Антирейд</CardTitle>
                <CardDescription>Защита от массовых набегов ботов и спамеров</CardDescription>
              </div>
            </div>
            <Switch 
              checked={settings.anti_raid.enabled} 
              onCheckedChange={(enabled) => handleAntiRaidChange({ enabled })}
            />
          </div>
        </CardHeader>
        
        {settings.anti_raid.enabled && (
          <CardContent className="pt-6 space-y-8">
            {raidStatus?.lockdownActive && (
              <div className="bg-destructive/10 border border-destructive/20 rounded-lg p-4 flex flex-col sm:flex-row gap-4 items-center justify-between">
                <div className="flex items-center gap-3">
                  <ShieldBan className="h-6 w-6 text-destructive shrink-0" />
                  <div>
                    <h4 className="font-semibold text-destructive">Атака обнаружена — чат заблокирован</h4>
                    <p className="text-sm text-destructive/80">
                      Новые участники не могут присоединиться. Попыток за окно: {raidStatus.joinsInWindow} / {raidStatus.joinThreshold}
                    </p>
                  </div>
                </div>
                <Button variant="destructive" onClick={handleLiftLockdown} disabled={liftRaidLockdown.isPending}>
                  {liftRaidLockdown.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Снять блокировку
                </Button>
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-6">
              <div className="space-y-3">
                <div className="flex justify-between">
                  <Label>Триггер (вхождений)</Label>
                  <span className="text-sm text-muted-foreground">{settings.anti_raid.join_threshold}</span>
                </div>
                <Slider 
                  value={[settings.anti_raid.join_threshold]} 
                  min={1} max={50} step={1}
                  onValueChange={([val]) => handleAntiRaidChange({ join_threshold: val })}
                />
                <p className="text-xs text-muted-foreground">Кол-во вхождений для активации блокировки</p>
              </div>

              <div className="space-y-3">
                <div className="flex justify-between">
                  <Label>Окно времени (сек)</Label>
                  <span className="text-sm text-muted-foreground">{settings.anti_raid.join_window}</span>
                </div>
                <Slider 
                  value={[settings.anti_raid.join_window]} 
                  min={5} max={300} step={5}
                  onValueChange={([val]) => handleAntiRaidChange({ join_window: val })}
                />
                <p className="text-xs text-muted-foreground">За какой период считаем вхождения</p>
              </div>

              <div className="space-y-3">
                <div className="flex justify-between">
                  <Label>Длительность блокировки (сек)</Label>
                  <span className="text-sm text-muted-foreground">{settings.anti_raid.lockdown_duration}</span>
                </div>
                <Slider 
                  value={[settings.anti_raid.lockdown_duration]} 
                  min={60} max={3600} step={60}
                  onValueChange={([val]) => handleAntiRaidChange({ lockdown_duration: val })}
                />
                <p className="text-xs text-muted-foreground">Как долго чат будет закрыт</p>
              </div>
              
              <div className="space-y-3">
                <div className="flex justify-between">
                  <Label>Окно сообщений (сек)</Label>
                  <span className="text-sm text-muted-foreground">{settings.anti_raid.msg_window}</span>
                </div>
                <Slider 
                  value={[settings.anti_raid.msg_window]} 
                  min={1} max={120} step={1}
                  onValueChange={([val]) => handleAntiRaidChange({ msg_window: val })}
                />
                <p className="text-xs text-muted-foreground">Окно для анализа одинаковых сообщений</p>
              </div>
            </div>

            <Separator />

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="flex items-center justify-between space-x-2 border rounded-md p-3">
                <div className="flex flex-col space-y-1">
                  <Label>Банить новых участников</Label>
                  <span className="text-[10px] text-muted-foreground">Банить тех, кто вошел во время флуда</span>
                </div>
                <Switch 
                  checked={settings.anti_raid.ban_new_joins}
                  onCheckedChange={(val) => handleAntiRaidChange({ ban_new_joins: val })}
                />
              </div>

              <div className="flex items-center justify-between space-x-2 border rounded-md p-3">
                <div className="flex flex-col space-y-1">
                  <Label>Ограничить чат</Label>
                  <span className="text-[10px] text-muted-foreground">Read-only во время блокировки</span>
                </div>
                <Switch 
                  checked={settings.anti_raid.restrict_chat}
                  onCheckedChange={(val) => handleAntiRaidChange({ restrict_chat: val })}
                />
              </div>

              <div className="flex items-center justify-between space-x-2 border rounded-md p-3">
                <div className="flex flex-col space-y-1">
                  <Label>Банить входящих в локдаун</Label>
                  <span className="text-[10px] text-muted-foreground">Банить всех, кто пытается зайти в закрытый чат</span>
                </div>
                <Switch 
                  checked={settings.anti_raid.ban_during_lockdown}
                  onCheckedChange={(val) => handleAntiRaidChange({ ban_during_lockdown: val })}
                />
              </div>

              <div className="flex items-center justify-between space-x-2 border rounded-md p-3">
                <div className="flex flex-col space-y-1">
                  <Label>Оповещать админов</Label>
                  <span className="text-[10px] text-muted-foreground">Присылать алёрт в чат</span>
                </div>
                <Switch 
                  checked={settings.anti_raid.notify_admins}
                  onCheckedChange={(val) => handleAntiRaidChange({ notify_admins: val })}
                />
              </div>

              <div className="flex items-center justify-between space-x-2 border rounded-md p-3">
                <div className="flex flex-col space-y-1">
                  <Label>Закрепить алерт</Label>
                  <span className="text-[10px] text-muted-foreground">Закрепить сообщение о начале рейда</span>
                </div>
                <Switch 
                  checked={settings.anti_raid.pin_alert}
                  onCheckedChange={(val) => handleAntiRaidChange({ pin_alert: val })}
                />
              </div>

              <div className="flex items-center justify-between space-x-2 border rounded-md p-3">
                <div className="flex flex-col space-y-1">
                  <Label>Банить за запретные теги</Label>
                  <span className="text-[10px] text-muted-foreground">Анализ юзернеймов и имен</span>
                </div>
                <Switch 
                  checked={settings.anti_raid.ban_for_tags}
                  onCheckedChange={(val) => handleAntiRaidChange({ ban_for_tags: val })}
                />
              </div>

              <div className="flex items-center justify-between space-x-2 border rounded-md p-3">
                <div className="flex flex-col space-y-1">
                  <Label>Удалять ссылки</Label>
                  <span className="text-[10px] text-muted-foreground">Удалять ссылки от не-админов в локдауне</span>
                </div>
                <Switch 
                  checked={settings.anti_raid.delete_links}
                  onCheckedChange={(val) => handleAntiRaidChange({ delete_links: val })}
                />
              </div>

              <div className="flex items-center justify-between space-x-2 border rounded-md p-3">
                <div className="flex flex-col space-y-1">
                  <Label>Анализ фото (ИИ)</Label>
                  <span className="text-[10px] text-muted-foreground">Запускать ИИ-анализ медиа при рейде</span>
                </div>
                <Switch 
                  checked={settings.anti_raid.analyze_photos}
                  onCheckedChange={(val) => handleAntiRaidChange({ analyze_photos: val })}
                />
              </div>
              
              <div className="flex items-center justify-between space-x-2 border border-primary/20 bg-primary/5 rounded-md p-3 col-span-1 md:col-span-2">
                <div className="flex flex-col space-y-1">
                  <Label className="text-primary">Режим тестирования</Label>
                  <span className="text-[10px] text-muted-foreground">Анализировать, но не наказывать</span>
                </div>
                <Switch 
                  checked={settings.anti_raid.test_mode}
                  onCheckedChange={(val) => handleAntiRaidChange({ test_mode: val })}
                />
              </div>
            </div>

            <Separator />

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
               <div className="space-y-2">
                  <Label className="text-xs">Лимит одинаковых тегов</Label>
                  <Input 
                    type="number" min={1} 
                    value={settings.anti_raid.same_tag_threshold}
                    onChange={(e) => handleAntiRaidChange({ same_tag_threshold: Number(e.target.value) })}
                  />
               </div>
               <div className="space-y-2">
                  <Label className="text-xs">Лимит одинаковых сообщений</Label>
                  <Input 
                    type="number" min={1} 
                    value={settings.anti_raid.same_msg_threshold}
                    onChange={(e) => handleAntiRaidChange({ same_msg_threshold: Number(e.target.value) })}
                  />
               </div>
               <div className="space-y-2">
                  <Label className="text-xs">Лимит одинаковых стикеров</Label>
                  <Input 
                    type="number" min={1} 
                    value={settings.anti_raid.same_sticker_threshold}
                    onChange={(e) => handleAntiRaidChange({ same_sticker_threshold: Number(e.target.value) })}
                  />
               </div>
            </div>

          </CardContent>
        )}
      </Card>

      {/* ----------------- АНТИСПАМ ----------------- */}
      <Card className="border-border shadow-sm">
        <CardHeader className="border-b border-border bg-card/50 pb-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-md bg-primary/10 flex items-center justify-center">
                <ShieldCheck className="h-5 w-5 text-primary" />
              </div>
              <div>
                <CardTitle className="text-xl">Антиспам</CardTitle>
                <CardDescription>Защита от флуда и дублирующихся сообщений</CardDescription>
              </div>
            </div>
            <Switch 
              checked={settings.antispam.enabled} 
              onCheckedChange={(enabled) => handleAntispamChange({ enabled })}
            />
          </div>
        </CardHeader>
        
        {settings.antispam.enabled && (
          <CardContent className="pt-6 space-y-6">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="space-y-2">
                <Label>Наказание</Label>
                <Select 
                  value={settings.antispam.punishment} 
                  onValueChange={(val: any) => handleAntispamChange({ punishment: val })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="мут">Мут (Read-only)</SelectItem>
                    <SelectItem value="бан">Бан (Удаление)</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label>Длительность</Label>
                <div className="flex gap-2">
                  <Input 
                    type="number" 
                    min={1} 
                    className="w-24"
                    value={settings.antispam.duration}
                    onChange={(e) => handleAntispamChange({ duration: Number(e.target.value) })}
                  />
                  <Select 
                    value={settings.antispam.unit} 
                    onValueChange={(val: any) => handleAntispamChange({ unit: val })}
                  >
                    <SelectTrigger className="flex-1">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="мин">Минут</SelectItem>
                      <SelectItem value="час">Часов</SelectItem>
                      <SelectItem value="день">Дней</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </div>

            <Separator />

            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              <div className="space-y-2">
                <Label className="text-sm">Лимит сообщений (флуд)</Label>
                <Input 
                  type="number" min={1} 
                  value={settings.antispam.threshold_count}
                  onChange={(e) => handleAntispamChange({ threshold_count: Number(e.target.value) })}
                />
                <p className="text-xs text-muted-foreground">Кол-во сообщений</p>
              </div>
              
              <div className="space-y-2">
                <Label className="text-sm">Окно времени (сек)</Label>
                <Input 
                  type="number" min={1} 
                  value={settings.antispam.threshold_seconds}
                  onChange={(e) => handleAntispamChange({ threshold_seconds: Number(e.target.value) })}
                />
                <p className="text-xs text-muted-foreground">За какой период</p>
              </div>

              <div className="space-y-2">
                <Label className="text-sm">Лимит дубликатов</Label>
                <Input 
                  type="number" min={1} 
                  value={settings.antispam.duplicate_limit}
                  onChange={(e) => handleAntispamChange({ duplicate_limit: Number(e.target.value) })}
                />
                <p className="text-xs text-muted-foreground">Одинаковых подряд</p>
              </div>
            </div>
          </CardContent>
        )}
      </Card>

      {/* ----------------- ЗАЩИТА ОТ 18+ ----------------- */}
      <Card className="border-border shadow-sm">
        <CardHeader className="border-b border-border bg-card/50 pb-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-md bg-orange-500/10 flex items-center justify-center">
                <ShieldBan className="h-5 w-5 text-orange-500" />
              </div>
              <div>
                <CardTitle className="text-xl">Защита от 18+</CardTitle>
                <CardDescription>Удаление порнографии и шок-контента</CardDescription>
              </div>
            </div>
            <Switch 
              checked={settings.antinsfw.enabled} 
              onCheckedChange={(enabled) => handleAntiNsfwChange({ enabled })}
            />
          </div>
        </CardHeader>
        
        {settings.antinsfw.enabled && (
          <CardContent className="pt-6">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="space-y-2">
                <Label>Наказание</Label>
                <Select 
                  value={settings.antinsfw.punishment} 
                  onValueChange={(val: any) => handleAntiNsfwChange({ punishment: val })}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="мут">Мут (Read-only)</SelectItem>
                    <SelectItem value="бан">Бан (Удаление)</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <Label>Длительность</Label>
                <div className="flex gap-2">
                  <Input 
                    type="number" 
                    min={1} 
                    className="w-24"
                    value={settings.antinsfw.duration}
                    onChange={(e) => handleAntiNsfwChange({ duration: Number(e.target.value) })}
                  />
                  <Select 
                    value={settings.antinsfw.unit} 
                    onValueChange={(val: any) => handleAntiNsfwChange({ unit: val })}
                  >
                    <SelectTrigger className="flex-1">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="мин">Минут</SelectItem>
                      <SelectItem value="час">Часов</SelectItem>
                      <SelectItem value="день">Дней</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </div>
          </CardContent>
        )}
      </Card>

      {/* ----------------- МОДЕРАТОРЫ ----------------- */}
      <Card className="border-border shadow-sm">
        <CardHeader className="border-b border-border bg-card/50 pb-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-md bg-secondary flex items-center justify-center">
              <Users className="h-5 w-5 text-secondary-foreground" />
            </div>
            <div>
              <CardTitle className="text-xl">Модераторы</CardTitle>
              <CardDescription>Администраторы чата, игнорируемые фильтрами</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="pt-0 p-0">
          <ScrollArea className="h-64">
            <div className="divide-y">
              {!moderators || moderators.length === 0 ? (
                <div className="p-8 text-center text-muted-foreground text-sm">
                  Нет данных о модераторах
                </div>
              ) : (
                moderators.map((mod) => (
                  <div key={mod.id} className="flex items-center justify-between p-4 hover:bg-muted/20 transition-colors">
                    <div className="flex items-center gap-3">
                      <Avatar className="h-9 w-9 border border-border">
                        {mod.photoUrl ? <AvatarImage src={mod.photoUrl} /> : null}
                        <AvatarFallback>{mod.name.charAt(0)}</AvatarFallback>
                      </Avatar>
                      <div className="flex flex-col">
                        <span className="text-sm font-medium">{mod.name}</span>
                        {mod.username && <span className="text-xs text-muted-foreground">@{mod.username}</span>}
                      </div>
                    </div>
                    <Badge variant={mod.status === 'creator' ? 'default' : 'secondary'} className="text-[10px] uppercase tracking-wider">
                      {mod.status === 'creator' ? 'Владелец' : 'Админ'}
                    </Badge>
                  </div>
                ))
              )}
            </div>
          </ScrollArea>
        </CardContent>
      </Card>

    </div>
  );
}
