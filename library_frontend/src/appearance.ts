import { api } from "./api";

export interface AppearanceSettings {
  backgroundColor: string;
  textColor: string;
  accentColor: string;
}

export interface AppearanceResponse {
  settings: AppearanceSettings;
  persisted: boolean;
}

export const DEFAULT_APPEARANCE_SETTINGS: AppearanceSettings = {
  backgroundColor: "#0a0c10",
  textColor: "#edf1f7",
  accentColor: "#3976da"
};

export const BUILTIN_APPEARANCE_PRESETS: Array<{ name: string; settings: AppearanceSettings }> = [
  { name: "기본 블루", settings: DEFAULT_APPEARANCE_SETTINGS },
  { name: "딥 퍼플", settings: { backgroundColor: "#0e0b14", textColor: "#f2edff", accentColor: "#7c3aed" } },
  { name: "포레스트", settings: { backgroundColor: "#08110d", textColor: "#e8f5ee", accentColor: "#168a55" } },
  { name: "슬레이트", settings: { backgroundColor: "#111827", textColor: "#f1f5f9", accentColor: "#0ea5e9" } },
  { name: "웜 브라운", settings: { backgroundColor: "#15100c", textColor: "#f7eee7", accentColor: "#c66a32" } }
];

const STORAGE_KEY = "file-check.library.appearance";
const HEX_COLOR_RE = /^#[0-9a-f]{6}$/i;

export function readAppearanceSettings(): AppearanceSettings {
  if (typeof window === "undefined") return DEFAULT_APPEARANCE_SETTINGS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_APPEARANCE_SETTINGS;
    return normalizeAppearanceSettings(JSON.parse(raw) as Partial<AppearanceSettings>);
  } catch {
    return DEFAULT_APPEARANCE_SETTINGS;
  }
}

export function hasStoredAppearanceSettings(): boolean {
  return typeof window !== "undefined" && window.localStorage.getItem(STORAGE_KEY) !== null;
}

export async function fetchAppearanceSettings(): Promise<AppearanceResponse> {
  const response = await api<AppearanceResponse>("/api/settings/appearance");
  return { settings: normalizeAppearanceSettings(response.settings), persisted: response.persisted };
}

export async function syncAppearanceSettingsFromServer(): Promise<AppearanceResponse> {
  const response = await fetchAppearanceSettings();
  if (response.persisted || !hasStoredAppearanceSettings()) {
    storeAndApply(response.settings);
  }
  return response;
}

export async function saveAppearanceSettings(settings: AppearanceSettings): Promise<AppearanceSettings> {
  const response = await api<AppearanceResponse>("/api/settings/appearance", {
    method: "PUT",
    body: JSON.stringify({ settings: normalizeAppearanceSettings(settings) })
  });
  const saved = normalizeAppearanceSettings(response.settings);
  storeAndApply(saved);
  return saved;
}

export async function resetAppearanceSettings(): Promise<AppearanceSettings> {
  const response = await api<AppearanceResponse>("/api/settings/appearance", { method: "DELETE" });
  const saved = normalizeAppearanceSettings(response.settings);
  storeAndApply(saved);
  return saved;
}

export function applyAppearanceSettings(settings: AppearanceSettings): void {
  if (typeof document === "undefined") return;
  const variables = buildCssVariables(normalizeAppearanceSettings(settings));
  for (const [name, value] of Object.entries(variables)) {
    document.documentElement.style.setProperty(name, value);
  }
}

export function normalizeHexColor(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  const trimmed = value.trim();
  return HEX_COLOR_RE.test(trimmed) ? trimmed.toLowerCase() : fallback;
}

export function normalizeAppearanceSettings(settings: Partial<AppearanceSettings>): AppearanceSettings {
  return {
    backgroundColor: normalizeHexColor(settings.backgroundColor, DEFAULT_APPEARANCE_SETTINGS.backgroundColor),
    textColor: normalizeHexColor(settings.textColor, DEFAULT_APPEARANCE_SETTINGS.textColor),
    accentColor: normalizeHexColor(settings.accentColor, DEFAULT_APPEARANCE_SETTINGS.accentColor)
  };
}

function storeAndApply(settings: AppearanceSettings): void {
  const normalized = normalizeAppearanceSettings(settings);
  if (typeof window !== "undefined") {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
  }
  applyAppearanceSettings(normalized);
}

type Rgb = { r: number; g: number; b: number };

function buildCssVariables(settings: AppearanceSettings): Record<string, string> {
  const background = hexToRgb(settings.backgroundColor);
  const text = hexToRgb(settings.textColor);
  const accent = hexToRgb(settings.accentColor);
  const surfaceTarget = luminance(background) > 0.45
    ? { r: 0, g: 0, b: 0 }
    : { r: 255, g: 255, b: 255 };
  const accentLight = mix(accent, text, 0.34);
  return {
    "--bg": settings.backgroundColor,
    "--panel": rgbToHex(mix(background, surfaceTarget, 0.055)),
    "--panel-2": rgbToHex(mix(background, surfaceTarget, 0.095)),
    "--surface-low": rgbToHex(mix(background, surfaceTarget, 0.035)),
    "--input": rgbToHex(mix(background, surfaceTarget, 0.07)),
    "--hover": rgbToHex(mix(background, surfaceTarget, 0.13)),
    "--line": rgbToHex(mix(background, surfaceTarget, 0.18)),
    "--line-strong": rgbToHex(mix(background, surfaceTarget, 0.27)),
    "--text": settings.textColor,
    "--muted": rgbToHex(mix(text, background, 0.55)),
    "--blue": rgbToHex(accentLight),
    "--blue-2": settings.accentColor,
    "--accent-on": luminance(accent) > 0.5 ? "#0a0c10" : "#ffffff",
    "--accent-soft": `rgba(${accent.r}, ${accent.g}, ${accent.b}, .18)`,
    "--accent-shadow": `rgba(${accent.r}, ${accent.g}, ${accent.b}, .26)`,
    "--bg-glow": rgbToHex(mix(background, accent, 0.2)),
    "--sidebar": `rgba(${background.r}, ${background.g}, ${background.b}, .94)`,
    "--panel-alpha": `rgba(${mix(background, surfaceTarget, 0.055).r}, ${mix(background, surfaceTarget, 0.055).g}, ${mix(background, surfaceTarget, 0.055).b}, .94)`
  };
}

function hexToRgb(hex: string): Rgb {
  const value = hex.slice(1);
  return {
    r: Number.parseInt(value.slice(0, 2), 16),
    g: Number.parseInt(value.slice(2, 4), 16),
    b: Number.parseInt(value.slice(4, 6), 16)
  };
}

function mix(base: Rgb, overlay: Rgb, overlayWeight: number): Rgb {
  return {
    r: Math.round(base.r * (1 - overlayWeight) + overlay.r * overlayWeight),
    g: Math.round(base.g * (1 - overlayWeight) + overlay.g * overlayWeight),
    b: Math.round(base.b * (1 - overlayWeight) + overlay.b * overlayWeight)
  };
}

function rgbToHex(rgb: Rgb): string {
  return `#${[rgb.r, rgb.g, rgb.b].map((value) => Math.max(0, Math.min(255, value)).toString(16).padStart(2, "0")).join("")}`;
}

function luminance(rgb: Rgb): number {
  const [r, g, b] = [rgb.r, rgb.g, rgb.b].map((channel) => {
    const normalized = channel / 255;
    return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
