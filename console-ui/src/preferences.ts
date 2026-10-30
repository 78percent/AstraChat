export type Theme = 'dark' | 'light' | 'system';
export const THEME_KEY = 'astrachat.theme.v1';
const LEGACY_KEY = 'mitsuha.kouri-console.mock.v1';
const isTheme = (value: unknown): value is Theme => value === 'dark' || value === 'light' || value === 'system';

export function readTheme(storage: Pick<Storage, 'getItem'>): Theme {
  const current = storage.getItem(THEME_KEY);
  if (isTheme(current)) return current;
  // Preserve the selected theme without rewriting or deleting old preview data.
  try {
    const legacy: unknown = JSON.parse(storage.getItem(LEGACY_KEY) || 'null');
    if (legacy && typeof legacy === 'object' && 'theme' in legacy && isTheme(legacy.theme)) return legacy.theme;
  } catch { /* Invalid old preview data does not block the real console. */ }
  return 'dark';
}
