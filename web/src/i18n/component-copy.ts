import { useCallback } from "react";
import { useI18n } from "./context";

const ru: Record<string, string> = {
  "reasoning": "Глубина рассуждений",
  "Off (no thinking)": "Без рассуждений",
  "Minimal": "Минимальная", "Low": "Низкая", "Medium": "Средняя",
  "High": "Высокая", "Extra High": "Очень высокая", "Max": "Максимальная", "Ultra": "Предельная",
  "Loading model info…": "Загрузка сведений о модели…",
  "Context Window": "Контекстное окно", "Max Output": "Максимальный ответ",
  "override — auto": "задано вручную; автоматически",
  "auto-detected": "определено автоматически",
  "Tools": "Инструменты", "Vision": "Изображения", "Reasoning": "Рассуждения",
  "Cancel": "Отмена", "Set up": "Настроить", "Schedule it": "Добавить в расписание",
  "{title} scheduled{when}": "В расписание добавлено: {title}{when}",
  "Couldn't load blueprints": "Не удалось загрузить шаблоны",
  "Loading blueprints…": "Загрузка шаблонов…",
  "No automation blueprints available.": "Шаблоны автоматизаций пока недоступны.",
  "Failed to load toolset config": "Не удалось загрузить настройки инструментов",
  "Post-setup complete": "Установка завершена",
  "Post-setup finished with errors": "Установка завершилась с ошибками",
  "Lost track of the post-setup process": "Потеряна связь с процессом установки",
  "enabled": "включено", "disabled": "выключено",
  "Failed to toggle toolset": "Не удалось переключить набор инструментов",
  "Provider set to {name}": "Выбран провайдер: {name}",
  "Failed to select provider": "Не удалось выбрать провайдера",
  "Enter at least one value to save": "Введите хотя бы одно значение для сохранения",
  "Saved {count} keys": "Сохранено ключей: {count}",
  "Nothing to save": "Нет изменений для сохранения",
  "Failed to save keys": "Не удалось сохранить ключи",
  "Failed to start post-setup": "Не удалось запустить установку",
  "Close": "Закрыть", "Active": "Активен", "Inactive": "Неактивен",
  "Enable toolset for {platform}": "Включить инструменты для {platform}",
  "Enabled for {platform}": "Включено для {platform}",
  "Disabled for {platform}": "Выключено для {platform}",
  "This toolset has no configurable backends — toggle it on or off above. It works with no provider selection or API keys.": "Этот набор инструментов не требует выбора провайдера или API-ключей. Используйте переключатель выше, чтобы включить или выключить его.",
  "No providers are available for this toolset in this install.": "В этой установке нет доступных провайдеров для данного набора инструментов.",
  "Selected": "Выбран", "Select": "Выбрать", "Saved": "Сохранено",
  "•••••••• (saved — leave blank to keep)": "•••••••• (сохранено; оставьте пустым, чтобы не менять)",
  "Get a key": "Получить ключ", "Save keys": "Сохранить ключи",
  "This backend needs a one-time install": "Для этого провайдера требуется однократная установка",
  ". Runs on this host — may take a few minutes.": ". Выполняется на этом сервере и может занять несколько минут.",
  "Installing…": "Установка…", "Run setup": "Запустить установку",
  "post-setup": "Установка", "Starting…": "Запуск…",
  "Hermes Console": "Консоль Hermes", "Close console": "Закрыть консоль",
  "idle": "Ожидание", "connecting": "Подключение", "connected": "Подключено", "ready": "Готово",
  "running": "Выполняется", "closed": "Закрыто", "error": "Ошибка",
};

/** Presentation only: never use localized labels as API values. */
export function useComponentCopy() {
  const { locale } = useI18n();
  return useCallback((english: string, values: Record<string, string | number> = {}) => {
    let text = locale === "ru" ? ru[english] ?? english : english;
    for (const [key, value] of Object.entries(values)) text = text.replaceAll(`{${key}}`, String(value));
    return text;
  }, [locale]);
}
