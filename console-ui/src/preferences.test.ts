import { describe, expect, it } from 'vitest';
import { readTheme, THEME_KEY } from './preferences';

describe('theme migration', () => {
  const storage = (values: Record<string, string>) => ({ getItem: (key: string) => values[key] ?? null });
  it('keeps the previous theme without carrying preview records into new settings', () => {
    const values = { 'mitsuha.kouri-console.mock.v1': JSON.stringify({ theme: 'light', contexts: { old: ['private'] } }) };
    const before = JSON.stringify(values);
    expect(readTheme(storage(values))).toBe('light');
    expect(JSON.stringify(values)).toBe(before);
  });
  it('prefers the new theme setting', () => {
    expect(readTheme(storage({ [THEME_KEY]: 'system', 'mitsuha.kouri-console.mock.v1': '{"theme":"light"}' }))).toBe('system');
  });
  it('falls back safely when preview data is missing or malformed', () => {
    for (const legacy of ['', 'null', 'broken', '{}', '{"theme":"invalid"}']) {
      expect(readTheme(storage({ 'mitsuha.kouri-console.mock.v1': legacy }))).toBe('dark');
    }
  });
});
