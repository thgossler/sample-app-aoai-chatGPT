import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { ThemeProvider, createTheme, Theme } from '@fluentui/react';

// US English: Color mode context for dark/light theme switching
export type ColorMode = 'light' | 'dark';

interface ColorModeContextType {
  mode: ColorMode;
  theme: Theme;
  toggle: () => void;
}

export const ColorModeContext = createContext<ColorModeContextType | undefined>(undefined);

const THEME_KEY = 'theme';

const getSystemMode = (): ColorMode =>
  window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';

const getStoredMode = (): ColorMode | null => {
  const stored = localStorage.getItem(THEME_KEY);
  return stored === 'dark' || stored === 'light' ? stored : null;
};

const getInitialMode = (): ColorMode => getStoredMode() ?? getSystemMode();

const lightTheme = createTheme({
  palette: {
    themePrimary: '#F7931E', // orange from logo
    themeLighterAlt: '#FFF3E0',
    themeLighter: '#FFB347',
    themeLight: '#FFD699',
    themeTertiary: '#FFB347',
    themeSecondary: '#FF8C00',
    themeDarkAlt: '#FF8C00',
    themeDark: '#B85C00',
    themeDarker: '#7A3E00',
    neutralLighterAlt: '#faf9f8',
    neutralLighter: '#f3f2f1',
    neutralLight: '#edebe9',
    neutralQuaternaryAlt: '#e1dfdd',
    neutralQuaternary: '#d0d0d0',
    neutralTertiaryAlt: '#c8c6c4',
    neutralTertiary: '#a19f9d',
    neutralSecondary: '#605e5c',
    neutralPrimaryAlt: '#3b3a39',
    neutralPrimary: '#323130',
    neutralDark: '#201f1e',
    black: '#000000',
    white: '#ffffff',
  },
  defaultFontStyle: { fontFamily: 'Segoe UI, Arial, sans-serif' },
});

const darkTheme = createTheme({
  palette: {
    themePrimary: '#F7931E',
    themeLighterAlt: '#3a2a1b',
    themeLighter: '#B85C00',
    themeLight: '#7A3E00',
    themeTertiary: '#FFB347',
    themeSecondary: '#FF8C00',
    themeDarkAlt: '#B85C00',
    themeDark: '#7A3E00',
    themeDarker: '#3a2a1b',
    neutralLighterAlt: '#23272f',
    neutralLighter: '#2c313a',
    neutralLight: '#353a45',
    neutralQuaternaryAlt: '#3e4450',
    neutralQuaternary: '#474d5b',
    neutralTertiaryAlt: '#505769',
    neutralTertiary: '#c8c6c4',
    neutralSecondary: '#d0d0d0',
    neutralPrimaryAlt: '#e1e1e1',
    neutralPrimary: '#f3f3f3',
    neutralDark: '#faf9f8',
    black: '#ffffff',
    white: '#1b1a19',
  },
  defaultFontStyle: { fontFamily: 'Segoe UI, Arial, sans-serif' },
});

export const ColorModeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [mode, setMode] = useState<ColorMode>(getInitialMode());

  // Listen for system theme changes
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handleChange = () => {
      if (!getStoredMode()) {
        setMode(getSystemMode());
      }
    };
    mq.addEventListener('change', handleChange);
    return () => mq.removeEventListener('change', handleChange);
  }, []);

  // Sync mode to localStorage
  useEffect(() => {
    const system = getSystemMode();
    if (getStoredMode() && mode === system) {
      localStorage.removeItem(THEME_KEY);
    } else if (mode !== system) {
      localStorage.setItem(THEME_KEY, mode);
    }
  }, [mode]);

  // Add/remove .light/.dark class to <body> for CSS theme compatibility
  useEffect(() => {
    const body = document.body;
    body.classList.remove('light', 'dark');
    body.classList.add(mode);
    // Optionally, also set on <html> for global selectors
    document.documentElement.classList.remove('light', 'dark');
    document.documentElement.classList.add(mode);
  }, [mode]);

  const toggle = useCallback(() => {
    setMode(m => (m === 'light' ? 'dark' : 'light'));
  }, []);

  const theme = useMemo(() => (mode === 'dark' ? darkTheme : lightTheme), [mode]);

  const value = useMemo(() => ({ mode, theme, toggle }), [mode, theme, toggle]);

  return (
    <ColorModeContext.Provider value={value}>
      <ThemeProvider theme={theme} applyTo="body">
        {children}
      </ThemeProvider>
    </ColorModeContext.Provider>
  );
};

export const useColorMode = (): ColorModeContextType => {
  const ctx = useContext(ColorModeContext);
  if (!ctx) throw new Error('useColorMode must be used within a ColorModeProvider');
  return ctx;
};

export {};
