import { theme, type ThemeConfig } from 'antd';

export type ThemeMode = 'light' | 'dark';

/**
 * Static fallbacks matching globals.css. The first client paint happens before
 * getComputedStyle can be trusted, and antd would otherwise compute its palette
 * from empty strings.
 */
const FALLBACK: Record<ThemeMode, Record<string, string>> = {
  light: {
    '--bg': '#ffffff',
    '--bg-subtle': '#f7f8fa',
    '--bg-inset': '#eef0f4',
    '--bg-overlay': '#ffffff',
    '--border': '#e3e6ec',
    '--fg': '#12141a',
    '--fg-muted': '#5b6472',
    '--accent': '#4f46e5',
    '--success': '#0e9f6e',
    '--danger': '#dc2b3f',
    '--warning': '#b45309',
  },
  dark: {
    '--bg': '#0f1115',
    '--bg-subtle': '#14171d',
    '--bg-inset': '#1b1f27',
    '--bg-overlay': '#171a21',
    '--border': '#262b35',
    '--fg': '#e8eaee',
    '--fg-muted': '#9aa3b2',
    '--accent': '#7c74ff',
    '--success': '#34d399',
    '--danger': '#f87171',
    '--warning': '#fbbf24',
  },
};

function read(name: string, mode: ThemeMode): string {
  if (typeof window === 'undefined') return FALLBACK[mode][name];
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || FALLBACK[mode][name];
}

/** Maps our design tokens onto antd, which ships inside the JDM editor. */
export function antdTheme(mode: ThemeMode): ThemeConfig {
  const token = (name: string) => read(name, mode);
  return {
    algorithm: mode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: token('--accent'),
      colorBgBase: token('--bg'),
      colorBgContainer: token('--bg'),
      colorBgElevated: token('--bg-overlay'),
      colorBgLayout: token('--bg-subtle'),
      colorBorder: token('--border'),
      colorBorderSecondary: token('--border'),
      colorText: token('--fg'),
      colorTextSecondary: token('--fg-muted'),
      colorSuccess: token('--success'),
      colorError: token('--danger'),
      colorWarning: token('--warning'),
      borderRadius: 8,
      fontFamily: 'var(--font-sans)',
      fontFamilyCode: 'var(--font-mono)',
      fontSize: 13,
    },
  };
}

export const THEME_STORAGE_KEY = 'jdm-studio-theme';

export function resolveInitialTheme(): ThemeMode {
  if (typeof window === 'undefined') return 'light';
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === 'light' || stored === 'dark') return stored;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
