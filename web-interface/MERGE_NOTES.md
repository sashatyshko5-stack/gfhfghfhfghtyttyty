# Что делать, если GitHub показывает конфликт при merge

В этой ветке больше нет `ai-defender-web-interface.zip`: архив удалён и добавлен в `.gitignore`, чтобы GitHub не конфликтовал из-за бинарного артефакта.

## Быстрая проверка локально

```bash
git status --short
node --check web-interface/app.js
python3 -m py_compile web-interface/server.py
```

## Если конфликт всё равно висит на GitHub

1. Обновите целевую ветку локально.
2. Выполните merge/rebase этой ветки поверх актуальной целевой ветки.
3. Для конфликта по `ai-defender-web-interface.zip` выбирайте удаление файла.
4. Для конфликтов в `web-interface/*` оставляйте текущую версию из этой ветки, потому что именно она содержит рабочий frontend/backend и локальные инструкции.

Команды:

```bash
git checkout <branch-with-web-panel>
git fetch origin
git merge origin/<target-branch>
git rm -f ai-defender-web-interface.zip 2>/dev/null || true
git add web-interface .gitignore
git commit
```
