import type { Translations } from "@/i18n/types";

const BUILTIN: Record<string, keyof Translations["app"]["nav"]> = {
  "/chat": "chat",
  "/sessions": "sessions",
  "/analytics": "analytics",
  "/models": "models",
  "/logs": "logs",
  "/cron": "cron",
  "/skills": "skills",
  "/plugins": "plugins",
  "/files": "files",
  "/mcp": "mcp",
  "/channels": "channels",
  "/webhooks": "webhooks",
  "/pairing": "pairing",
  "/system": "system",
  "/profiles": "profiles",
  "/config": "config",
  "/env": "keys",
  "/docs": "documentation",
};

const BUILTIN_DEFAULT_LABELS: Partial<Record<keyof Translations["app"]["nav"], string>> = {
  files: "Files",
  mcp: "MCP",
  channels: "Channels",
  webhooks: "Webhooks",
  pairing: "Pairing",
  system: "System",
};

export function resolvePageTitle(
  pathname: string,
  t: Translations,
  pluginTabs: { path: string; label: string }[],
): string {
  const normalized = pathname.replace(/\/$/, "") || "/";
  if (normalized === "/") {
    return t.app.nav.sessions;
  }
  const plugin = pluginTabs.find((p) => p.path === normalized);
  if (plugin) {
    const pluginLabel = plugin.label.trim().toLowerCase();
    if (pluginLabel === "kanban") return t.app.pluginLabels?.kanban ?? plugin.label;
    if (pluginLabel === "achievements") return t.app.pluginLabels?.achievements ?? plugin.label;
    return plugin.label;
  }
  const key = BUILTIN[normalized];
  if (key) {
    return (t.app.nav as Record<string, string | undefined>)[key] ??
      BUILTIN_DEFAULT_LABELS[key] ??
      normalized.slice(1).charAt(0).toUpperCase() + normalized.slice(2);
  }
  // Derive title from pathname: "/profiles" → "Profiles"
  const segment = normalized.slice(1);
  if (segment) {
    return segment.charAt(0).toUpperCase() + segment.slice(1);
  }
  return t.app.webUi;
}
