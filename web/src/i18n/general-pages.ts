import type { Locale } from "./types";

/** Russian UI copy for small static labels on general dashboard pages. */
const russian: Record<string, string> = {
  "Jump to section": "Перейти к разделу",
  Providers: "Провайдеры",
  Tools: "Инструменты",
  Settings: "Настройки",
  "Hide {key}": "Скрыть {key}",
  "Reveal {key}": "Показать {key}",
  "Saved key: {key}": "Переменная {key} сохранена",
  "Removed key: {key}": "Переменная {key} удалена",
  "Refused: {reason}.": "Запрос отклонён: {reason}.",
  "Chat websocket unavailable: {reason}.": "Веб-сокет чата недоступен: {reason}.",
  "Tool loop guardrails": "Ограничители цикла инструментов",
  "Tool output": "Вывод инструментов",
  "Model catalog": "Каталог моделей",
  Openrouter: "OpenRouter",
  Curator: "Куратор навыков",
  Kanban: "Канбан",
  Sessions: "Сессии",
  Bedrock: "Bedrock",
  Updates: "Обновления",
  Auxiliary: "Вспомогательные настройки",
};

export function generalPageText(
  locale: Locale,
  english: string,
  values: Record<string, string | number> = {},
): string {
  let result = locale === "ru" ? (russian[english] ?? english) : english;
  for (const [key, value] of Object.entries(values)) {
    result = result.replaceAll(`{${key}}`, String(value));
  }
  return result;
}

export function fieldCountText(locale: Locale, count: number): string {
  if (locale !== "ru") return `${count} field${count === 1 ? "" : "s"}`;
  const mod10 = count % 10;
  const mod100 = count % 100;
  const noun = mod10 === 1 && mod100 !== 11
    ? "поле"
    : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)
      ? "поля"
      : "полей";
  return `${count} ${noun}`;
}

export function keyCountText(locale: Locale, count: number): string {
  if (locale !== "ru") return `${count} key${count === 1 ? "" : "s"}`;
  const mod10 = count % 10;
  const mod100 = count % 100;
  const noun = mod10 === 1 && mod100 !== 11
    ? "ключ"
    : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)
      ? "ключа"
      : "ключей";
  return `${count} ${noun}`;
}

export function fieldsResetText(locale: Locale, count: number): string {
  if (locale === "ru") return `Будет сброшено ${fieldCountText(locale, count)} к значениям по умолчанию.`;
  return `This will reset ${fieldCountText(locale, count)} to their default values.`;
}
