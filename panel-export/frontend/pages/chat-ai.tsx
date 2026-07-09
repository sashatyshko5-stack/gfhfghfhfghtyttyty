import React, { useState } from 'react';
import { 
  useGetChatAi, 
  useUpdateChatAi, 
  useListAiProviders,
  useSetAiApiKey,
  useDeleteAiApiKey,
  getGetChatAiQueryKey,
  Personality,
  AiProviderId
} from '@workspace/api-client-react';
import { useQueryClient } from '@tanstack/react-query';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { Loader2, Bot, ChevronLeft, ShieldCheck, ShieldAlert } from '@/components/icons';
import { useToast } from '@/hooks/use-toast';
import { Separator } from '@/components/ui/separator';

interface ChatAiProps {
  chatId: string;
  onBack: () => void;
}

export function ChatAi({ chatId, onBack }: ChatAiProps) {
  const { data: aiSettings, isLoading: isLoadingSettings, isError: isErrorSettings } = useGetChatAi(chatId);
  const { data: providers, isLoading: isLoadingProviders, isError: isErrorProviders } = useListAiProviders();
  
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const updateAi = useUpdateChatAi();
  const setKey = useSetAiApiKey();
  const deleteKey = useDeleteAiApiKey();

  const [apiKeysInput, setApiKeysInput] = useState<Record<string, string>>({});

  if (isLoadingSettings || isLoadingProviders) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  if (isErrorSettings || isErrorProviders || !aiSettings || !providers) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground p-8 text-center">
        <ShieldAlert className="h-10 w-10 text-destructive" />
        <p className="font-medium text-foreground">Не удалось загрузить настройки ИИ</p>
        <p className="text-sm">Проверьте подключение и попробуйте обновить страницу.</p>
      </div>
    );
  }

  const patchSettings = (updater: (old: typeof aiSettings) => typeof aiSettings) => {
    queryClient.setQueryData(getGetChatAiQueryKey(chatId), (old: typeof aiSettings | undefined) => {
      if (!old) return old;
      return updater(old);
    });
  };

  const handleUpdate = (data: Parameters<typeof updateAi.mutate>[0]['data']) => {
    // Optimistic update
    patchSettings(old => ({ 
      ...old, 
      ...data,
      custom_provider: data.custom_provider ? { ...old.custom_provider, ...data.custom_provider } : old.custom_provider
    }));
    
    updateAi.mutate({ chatId, data }, {
      onSuccess: () => toast({ title: "Сохранено", description: "Настройки ИИ обновлены" }),
      onError: () => {
        toast({ title: "Ошибка", description: "Не удалось сохранить настройки", variant: "destructive" });
        queryClient.invalidateQueries({ queryKey: getGetChatAiQueryKey(chatId) });
      }
    });
  };

  const handleSetKey = (providerId: AiProviderId) => {
    const key = apiKeysInput[providerId];
    if (!key) return;

    setKey.mutate({ chatId, data: { provider: providerId, api_key: key } }, {
      onSuccess: () => {
        toast({ title: "Ключ сохранён", description: `API ключ для ${providerId} успешно добавлен` });
        setApiKeysInput(prev => ({ ...prev, [providerId]: '' })); // clear input
        patchSettings(old => ({
          ...old,
          providerKeys: providerId !== 'custom' ? { ...old.providerKeys, [providerId]: true } : old.providerKeys,
          custom_provider: providerId === 'custom' ? { ...old.custom_provider, hasKey: true } : old.custom_provider
        }));
      },
      onError: () => toast({ title: "Ошибка", description: "Не удалось сохранить ключ", variant: "destructive" })
    });
  };

  const handleDeleteKey = (providerId: AiProviderId) => {
    deleteKey.mutate({ chatId, provider: providerId }, {
      onSuccess: () => {
        toast({ title: "Ключ удалён", description: `API ключ для ${providerId} удалён` });
        patchSettings(old => ({
          ...old,
          providerKeys: providerId !== 'custom' ? { ...old.providerKeys, [providerId]: false } : old.providerKeys,
          custom_provider: providerId === 'custom' ? { ...old.custom_provider, hasKey: false } : old.custom_provider
        }));
      },
      onError: () => toast({ title: "Ошибка", description: "Не удалось удалить ключ", variant: "destructive" })
    });
  };

  const selectedProvider = providers.find(p => p.id === aiSettings.ai_provider);
  const isCustomProvider = aiSettings.ai_provider === 'custom';

  return (
    <div className="max-w-4xl mx-auto p-4 md:p-8 space-y-6 pb-24">
      <div className="flex items-center gap-4 mb-6">
        <Button variant="outline" size="icon" onClick={onBack}>
          <ChevronLeft className="h-4 w-4" />
        </Button>
        <div>
          <h2 className="text-2xl font-bold tracking-tight">Искусственный Интеллект</h2>
          <p className="text-muted-foreground text-sm">Настройка генерации ответов и анализа</p>
        </div>
      </div>

      <Card className="border-border shadow-sm">
        <CardHeader className="border-b border-border bg-card/50 pb-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-md bg-primary/10 flex items-center justify-center">
                <Bot className="h-5 w-5 text-primary" />
              </div>
              <div>
                <CardTitle className="text-xl">ИИ Ассистент</CardTitle>
                <CardDescription>Бот может общаться в чате, используя выбранную личность</CardDescription>
              </div>
            </div>
            <Switch 
              checked={aiSettings.ai_enabled} 
              onCheckedChange={(enabled) => handleUpdate({ ai_enabled: enabled })}
            />
          </div>
        </CardHeader>
        
        {aiSettings.ai_enabled && (
          <CardContent className="pt-6 space-y-8">
            <div className="space-y-4">
              <Label className="text-base font-semibold">Личность (Характер)</Label>
              <Select 
                value={aiSettings.personality} 
                onValueChange={(val: Personality) => handleUpdate({ personality: val })}
              >
                <SelectTrigger className="w-full md:w-[300px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.values(Personality).map(p => (
                    <SelectItem key={p} value={p} className="capitalize">{p}</SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {aiSettings.personality === 'кастомный' && (
                <div className="space-y-2 mt-4 animate-in fade-in slide-in-from-top-2">
                  <Label>Системный промпт (инструкция)</Label>
                  <Textarea 
                    placeholder="Ты администратор этого чата. Общайся вежливо, но строго..."
                    className="h-32 resize-y"
                    value={aiSettings.custom}
                    onChange={(e) => handleUpdate({ custom: e.target.value })} // Note: might want to debounce this in a real app, but for now direct onChange is requested pattern without complex local state
                  />
                  <p className="text-xs text-muted-foreground">Опишите, как ИИ должен себя вести, какие правила соблюдать.</p>
                </div>
              )}
            </div>

            <Separator />

            <div className="space-y-6">
              <div>
                <Label className="text-base font-semibold">Модель и Провайдер</Label>
                <p className="text-sm text-muted-foreground mb-4">Выберите нейросеть, которая будет обрабатывать запросы.</p>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div className="space-y-2">
                  <Label>Провайдер API</Label>
                  <Select 
                    value={aiSettings.ai_provider} 
                    onValueChange={(val: AiProviderId) => {
                      const newProv = providers.find(p => p.id === val);
                      handleUpdate({ 
                        ai_provider: val,
                        ai_model: newProv?.defaultModel || (newProv?.models.length ? newProv.models[0] : '')
                      });
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {providers.map(p => (
                        <SelectItem key={p.id} value={p.id}>{p.label}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                {!isCustomProvider ? (
                  <div className="space-y-2">
                    <Label>Модель</Label>
                    <Select 
                      value={aiSettings.ai_model} 
                      onValueChange={(val) => handleUpdate({ ai_model: val })}
                      disabled={!selectedProvider?.models.length}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Выберите модель..." />
                      </SelectTrigger>
                      <SelectContent>
                        {selectedProvider?.models.map(m => (
                          <SelectItem key={m} value={m}>{m}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                ) : (
                  <div className="space-y-4 col-span-1 md:col-span-2">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      <div className="space-y-2">
                        <Label>Endpoint URL (OpenAI совместимый)</Label>
                        <Input 
                          placeholder="https://api.openai.com/v1/chat/completions"
                          value={aiSettings.custom_provider?.endpoint || ''}
                          onChange={(e) => handleUpdate({ custom_provider: { endpoint: e.target.value } })}
                        />
                      </div>
                      <div className="space-y-2">
                        <Label>Имя модели</Label>
                        <Input 
                          placeholder="gpt-4-turbo"
                          value={aiSettings.custom_provider?.model || ''}
                          onChange={(e) => handleUpdate({ custom_provider: { model: e.target.value } })}
                        />
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
            
            <Separator />

            <div className="space-y-4">
              <div>
                <Label className="text-base font-semibold">API Ключ ({selectedProvider?.label || 'Custom'})</Label>
                <p className="text-sm text-muted-foreground mb-4">Для работы ИИ необходим ключ доступа к выбранному провайдеру.</p>
              </div>

              {/* Status Badge */}
              <div className="mb-4">
                {(isCustomProvider ? aiSettings.custom_provider?.hasKey : aiSettings.providerKeys[aiSettings.ai_provider]) ? (
                  <Badge variant="default" className="bg-emerald-500/10 text-emerald-600 hover:bg-emerald-500/20 border-emerald-500/20 px-3 py-1">
                    <ShieldCheck className="w-3 h-3 mr-1" />
                    Ключ сохранён
                  </Badge>
                ) : (
                  <Badge variant="destructive" className="bg-destructive/10 text-destructive hover:bg-destructive/20 border-destructive/20 px-3 py-1">
                    <ShieldAlert className="w-3 h-3 mr-1" />
                    Ключ не установлен
                  </Badge>
                )}
              </div>

              <div className="flex gap-2 max-w-md">
                <Input 
                  type="password" 
                  placeholder="Введите новый API ключ..." 
                  value={apiKeysInput[aiSettings.ai_provider] || ''}
                  onChange={(e) => setApiKeysInput({ ...apiKeysInput, [aiSettings.ai_provider]: e.target.value })}
                  className="font-mono text-sm"
                />
                <Button 
                  onClick={() => handleSetKey(aiSettings.ai_provider)}
                  disabled={!apiKeysInput[aiSettings.ai_provider] || setKey.isPending}
                >
                  {setKey.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Сохранить
                </Button>
                {(isCustomProvider ? aiSettings.custom_provider?.hasKey : aiSettings.providerKeys[aiSettings.ai_provider]) && (
                  <Button 
                    variant="outline" 
                    className="border-destructive/50 text-destructive hover:bg-destructive hover:text-destructive-foreground"
                    onClick={() => handleDeleteKey(aiSettings.ai_provider)}
                    disabled={deleteKey.isPending}
                  >
                    Удалить
                  </Button>
                )}
              </div>
            </div>

          </CardContent>
        )}
      </Card>
    </div>
  );
}
