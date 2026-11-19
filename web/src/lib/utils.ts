import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Mondwest font only — use on layout shells; do not force normal-case here or `text-display` chrome (Segmented, badges) stops uppercasing. */
export const themedFont = "font-mondwest";

/** Mondwest body copy — sentence-case themed text (not uppercase chrome). */
export const themedBody = "font-mondwest normal-case";

/** Mondwest brand chrome — uppercase section headers and nav labels. */
export const themedChrome = "font-mondwest text-display";

function relativeTime(value: number, unit: Intl.RelativeTimeFormatUnit, locale: string): string {
  return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(-value, unit);
}

/** Relative time from a Unix epoch timestamp (seconds). Defaults to legacy English output. */
export function timeAgo(ts: number, locale = "en"): string {
  const delta = Date.now() / 1000 - ts;
  if (locale === "en") {
    if (delta < 60) return "just now";
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
    if (delta < 172800) return "yesterday";
    return `${Math.floor(delta / 86400)}d ago`;
  }
  if (!Number.isFinite(delta)) return locale === "ru" ? "неизвестно" : "unknown";
  if (delta < 60) return relativeTime(0, "second", locale);
  if (delta < 3600) return relativeTime(Math.floor(delta / 60), "minute", locale);
  if (delta < 86400) return relativeTime(Math.floor(delta / 3600), "hour", locale);
  return relativeTime(Math.floor(delta / 86400), "day", locale);
}

/** Relative time from an ISO-8601 timestamp string. Defaults to legacy English output. */
export function isoTimeAgo(iso: string, locale = "en"): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 0 || Number.isNaN(delta)) return locale === "ru" ? "неизвестно" : "unknown";
  if (locale === "en") {
    if (delta < 60) return "just now";
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
    return `${Math.floor(delta / 86400)}d ago`;
  }
  if (delta < 60) return relativeTime(0, "second", locale);
  if (delta < 3600) return relativeTime(Math.floor(delta / 60), "minute", locale);
  if (delta < 86400) return relativeTime(Math.floor(delta / 3600), "hour", locale);
  return relativeTime(Math.floor(delta / 86400), "day", locale);
}
