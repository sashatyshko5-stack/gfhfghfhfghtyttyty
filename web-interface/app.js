const CONFIG = window.AI_DEFENDER_CONFIG || {};
const API = CONFIG.apiBase || '';
const PROVIDERS = {
  laozhang: ['gpt-4o-mini', 'gpt-4o', 'deepseek-chat'],
  openrouter: ['meta-llama/llama-3.3-70b-instruct:free', 'deepseek/deepseek-r1:free', 'google/gemini-2.0-flash-exp:free'],
  google: ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-2.0-flash'],
  huggingface: ['Qwen/Qwen2.5-72B-Instruct', 'meta-llama/Llama-3.3-70B-Instruct'],
  groq: ['meta-llama/llama-4-scout-17b-16e-instruct', 'llama-3.3-70b-versatile'],
  custom: [],
};
const DEFAULT_SETTINGS = {
  antispam: { enabled: true, punishment: 'мут', duration: 30, unit: 'мин', test_mode: false, threshold_count: 5, threshold_seconds: 10, duplicate_limit: 3, types: { text: true, sticker: true, gif: true, photo: true, video: true, document: true, voice: true } },
  antiraid: { enabled: false, join_threshold: 5, join_window: 300, lockdown_duration: 600, ban_new_joins: true, restrict_chat: true, ban_during_lockdown: true, notify_admins: true, pin_alert: true, ban_for_tags: true, delete_links: true, analyze_photos: true, same_tag_threshold: 3, same_msg_threshold: 4, same_sticker_threshold: 5, msg_window: 60, test_mode: false },
  antinsfw: { enabled: false, punishment: 'мут', duration: 30, unit: 'мин' },
  ai_enabled: true, ai_provider: 'laozhang', ai_model: 'gpt-4o-mini', ai_keys: {}, custom_provider: { endpoint: '', api_key: '', model: '' },
};
const $ = id => document.getElementById(id);
let backendOnline = false;
let state = { user: null, chats: [], selected: null, page: 'settings' };

const localState = () => JSON.parse(localStorage.getItem('aiDefenderPanel') || 'null');
const saveLocal = () => localStorage.setItem('aiDefenderPanel', JSON.stringify(state));
const current = () => state.chats.find(c => c.id === state.selected);
const mergeDefaults = s => ({ ...structuredClone(DEFAULT_SETTINGS), ...(s || {}), antispam: { ...DEFAULT_SETTINGS.antispam, ...(s?.antispam || {}), types: { ...DEFAULT_SETTINGS.antispam.types, ...(s?.antispam?.types || {}) } }, antiraid: { ...DEFAULT_SETTINGS.antiraid, ...(s?.antiraid || {}) }, antinsfw: { ...DEFAULT_SETTINGS.antinsfw, ...(s?.antinsfw || {}) }, ai_keys: { ...(s?.ai_keys || {}) }, custom_provider: { ...DEFAULT_SETTINGS.custom_provider, ...(s?.custom_provider || {}) } });

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!response.ok) throw new Error(await response.text());
  return response.status === 204 ? null : response.json();
}
async function loadFromBackend() {
  try {
    const data = await api('/api/chats');
    backendOnline = true;
    state.chats = data.chats.map(c => ({ id: String(c.id), title: c.title || `Чат ${c.id}`, settings: mergeDefaults(c.settings) }));
    if (!state.chats.length) state.chats.push({ id: '-1001234567890', title: 'Тестовый чат', settings: structuredClone(DEFAULT_SETTINGS) });
    state.selected = state.selected || state.chats[0]?.id;
  } catch {
    backendOnline = false;
    const saved = localState();
    state.chats = saved?.chats || [{ id: '-1001234567890', title: 'Тестовый чат', settings: structuredClone(DEFAULT_SETTINGS) }];
    state.selected = saved?.selected || state.chats[0]?.id;
  }
}
async function saveToBackend(chat) {
  if (!backendOnline) return false;
  await api(`/api/chats/${encodeURIComponent(chat.id)}/settings`, { method: 'PUT', body: JSON.stringify({ title: chat.title, settings: chat.settings }) });
  return true;
}

async function initAuth() {
  const tg = window.Telegram?.WebApp;
  const saved = localState();
  if (tg?.initDataUnsafe?.user) {
    const u = tg.initDataUnsafe.user;
    state.user = { id: u.id, name: [u.first_name, u.last_name].filter(Boolean).join(' '), username: u.username || '' };
    tg.ready();
    await loadFromBackend();
    showApp();
    return;
  }
  if (saved?.user?.telegramAuth) {
    state.user = saved.user;
    await loadFromBackend();
    showApp();
    return;
  }
  renderBrowserTelegramLogin();
}
function showApp() {
  saveLocal();
  $('auth').classList.add('hidden'); $('app').classList.remove('hidden');
  $('profile-name').textContent = state.user.name || 'Admin';
  $('profile-username').textContent = '@' + (state.user.username || 'telegram');
  $('profile-avatar').textContent = (state.user.name || 'A')[0].toUpperCase();
  $('page-subtitle').textContent = backendOnline ? 'Настройки применяются в JSON-хранилище бота.' : 'Демо-режим браузера: сохранение в localStorage + экспорт JSON.';
  renderChats(); renderPage();
}
function renderChats() {
  const q = $('chat-search').value?.toLowerCase() || '';
  $('chat-list').innerHTML = '';
  state.chats.filter(c => c.title.toLowerCase().includes(q) || c.id.includes(q)).forEach(c => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'chat-item ' + (c.id === state.selected ? 'active' : '');
    b.innerHTML = `<div class="chat-avatar">${c.title[0]?.toUpperCase() || 'Ч'}</div><div class="chat-meta"><strong>${c.title}</strong><span>${c.id}</span></div>`;
    b.onclick = () => { state.selected = c.id; state.page = 'settings'; renderPage(); renderChats(); $('sidebar').classList.remove('open'); };
    $('chat-list').append(b);
  });
}
function get(path) { let o = current()?.settings; for (const p of path) o = o?.[p]; return o; }
function set(path, val) { let o = current().settings; for (const p of path.slice(0, -1)) { if (!o[p]) o[p] = {}; o = o[p]; } o[path.at(-1)] = val; }
function field(kind, label, path, opts = {}) {
  const val = get(path), id = path.join('__');
  if (kind === 'bool') return `<label class="switch-row"><span>${label}</span><input id="${id}" type="checkbox" ${val ? 'checked' : ''}></label>`;
  if (kind === 'select') return `<label>${label}<select id="${id}" class="field">${opts.values.map(v => `<option ${v === val ? 'selected' : ''}>${v}</option>`).join('')}</select></label>`;
  return `<label>${label}<input id="${id}" class="field" type="${kind === 'num' ? 'number' : 'text'}" value="${val ?? ''}"></label>`;
}
function bindAll() { document.querySelectorAll('[id*="__"]').forEach(el => { el.oninput = () => set(el.id.split('__'), el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value); }); }
function renderSettings() {
  const c = current(); $('empty-state').classList.toggle('hidden', !!c); $('settings-form').classList.toggle('hidden', !c); $('ai-form').classList.add('hidden'); if (!c) return;
  $('page-title').textContent = c.title; $('chat-title').value = c.title; $('chat-id').value = c.id; $('chat-title').oninput = e => { c.title = e.target.value; renderChats(); };
  $('antispam-fields').innerHTML = [field('bool', 'Включён', ['antispam', 'enabled']), field('bool', 'Тестовый режим', ['antispam', 'test_mode']), field('select', 'Наказание', ['antispam', 'punishment'], { values: ['мут', 'бан'] }), field('num', 'Длительность', ['antispam', 'duration']), field('select', 'Ед. времени', ['antispam', 'unit'], { values: ['сек', 'мин', 'час', 'дн'] }), field('num', 'Порог сообщений', ['antispam', 'threshold_count']), field('num', 'Окно, сек', ['antispam', 'threshold_seconds']), field('num', 'Лимит дублей', ['antispam', 'duplicate_limit']), ...Object.keys(DEFAULT_SETTINGS.antispam.types).map(k => field('bool', `Тип: ${k}`, ['antispam', 'types', k]))].join('');
  $('antiraid-fields').innerHTML = Object.entries({ enabled: 'Включён', ban_new_joins: 'Бан новых', restrict_chat: 'Закрывать чат', ban_during_lockdown: 'Бан при локдауне', notify_admins: 'Уведомлять админов', pin_alert: 'Закреплять тревогу', ban_for_tags: 'Бан за теги', delete_links: 'Удалять ссылки', analyze_photos: 'Анализ фото', test_mode: 'Тестовый режим' }).map(([k, l]) => field('bool', l, ['antiraid', k])).join('') + ['join_threshold', 'join_window', 'lockdown_duration', 'same_tag_threshold', 'same_msg_threshold', 'same_sticker_threshold', 'msg_window'].map(k => field('num', k, ['antiraid', k])).join('');
  $('nsfw-fields').innerHTML = [field('bool', 'Включена', ['antinsfw', 'enabled']), field('select', 'Наказание', ['antinsfw', 'punishment'], { values: ['мут', 'бан'] }), field('num', 'Длительность', ['antinsfw', 'duration']), field('select', 'Ед. времени', ['antinsfw', 'unit'], { values: ['сек', 'мин', 'час', 'дн'] })].join(''); bindAll();
}
function renderAi() {
  const c = current(); $('empty-state').classList.add('hidden'); $('settings-form').classList.add('hidden'); $('ai-form').classList.remove('hidden'); $('page-title').textContent = 'ИИ';
  $('ai-chat-select').innerHTML = state.chats.map(ch => `<option value="${ch.id}" ${ch.id === state.selected ? 'selected' : ''}>${ch.title}</option>`).join(''); $('ai-chat-select').onchange = e => { state.selected = e.target.value; renderAi(); };
  if (!c) { $('ai-fields').innerHTML = '<p>Сначала добавьте чат.</p>'; return; }
  const s = c.settings;
  $('ai-fields').innerHTML = `${field('bool', 'ИИ включён', ['ai_enabled'])}${field('select', 'Провайдер', ['ai_provider'], { values: Object.keys(PROVIDERS) })}<label>Модель<input id="ai_model" class="field" list="model-list" value="${s.ai_model || ''}"><datalist id="model-list">${(PROVIDERS[s.ai_provider] || []).map(m => `<option value="${m}">`).join('')}</datalist></label><h3>API ключи</h3>${Object.keys(PROVIDERS).filter(p => p !== 'custom').map(p => field('text', p, ['ai_keys', p])).join('')}<h3>Custom provider</h3>${field('text', 'Endpoint', ['custom_provider', 'endpoint'])}${field('text', 'API key', ['custom_provider', 'api_key'])}${field('text', 'Model', ['custom_provider', 'model'])}`;
  bindAll(); $('ai_model').oninput = e => { s.ai_model = e.target.value; };
}
function renderPage() { state.chats.forEach(c => c.settings = mergeDefaults(c.settings)); state.page === 'ai' ? renderAi() : renderSettings(); saveLocal(); }

function renderBrowserTelegramLogin() {
  $('auth').classList.remove('hidden');
  $('app').classList.add('hidden');
  window.onTelegramAuth = async user => {
    state.user = { id: user.id, name: [user.first_name, user.last_name].filter(Boolean).join(' '), username: user.username || '', photo_url: user.photo_url || '', telegramAuth: true };
    await loadFromBackend();
    showApp();
  };
  const widget = $('telegram-login-widget');
  const bot = CONFIG.telegramBotUsername || 'defende125_bot';
  widget.innerHTML = '';
  const script = document.createElement('script');
  script.async = true;
  script.src = 'https://telegram.org/js/telegram-widget.js?22';
  script.setAttribute('data-telegram-login', bot);
  script.setAttribute('data-size', 'large');
  script.setAttribute('data-radius', '12');
  script.setAttribute('data-request-access', 'write');
  script.setAttribute('data-onauth', 'onTelegramAuth(user)');
  widget.append(script);
}
$('tg-login').onclick = () => window.open(`https://t.me/${CONFIG.telegramBotUsername || 'defende125_bot'}`, '_blank');
$('ai-page').onclick = () => { state.page = 'ai'; if (!state.selected && state.chats[0]) state.selected = state.chats[0].id; renderPage(); };
$('save-settings').onclick = async () => { const c = current(); if (!c) return; saveLocal(); try { const applied = await saveToBackend(c); alert(applied ? 'Настройки применены в JSON-хранилище бота.' : 'Демо-режим: настройки сохранены локально, экспортируйте JSON.'); } catch (e) { alert(`Не удалось применить на backend: ${e.message}`); } };
$('add-chat').onclick = () => { const id = prompt('ID чата Telegram'); if (!id) return; state.chats.push({ id, title: 'Новый чат', settings: structuredClone(DEFAULT_SETTINGS) }); state.selected = id; renderPage(); renderChats(); };
$('collapse-sidebar').onclick = () => $('sidebar').classList.toggle('collapsed'); $('mobile-menu').onclick = () => $('sidebar').classList.add('open'); $('mobile-close').onclick = () => $('sidebar').classList.remove('open'); $('chat-search').oninput = renderChats;
$('export-json').onclick = () => { const data = Object.fromEntries(state.chats.map(c => [c.id, c.settings])); const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })); a.download = 'settings.json'; a.click(); };
$('import-json').onclick = () => $('file-picker').click(); $('file-picker').onchange = async e => { const file = e.target.files[0]; if (!file) return; const json = JSON.parse(await file.text()); state.chats = Object.entries(json).map(([id, settings]) => ({ id, title: `Чат ${id}`, settings: mergeDefaults(settings) })); state.selected = state.chats[0]?.id; renderChats(); renderPage(); };
initAuth();
