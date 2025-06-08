import React from 'react';
import { useColorMode } from '../../theme/useColorMode';

import styles from './ThemeToggle.module.css'

// US English: Theme toggle component for dark/light mode
const ThemeToggle: React.FC = () => {
  const { mode, toggle } = useColorMode();
  return (
    <button
      className={styles.themeToggleIconButton}
      onClick={toggle}
      aria-label="Toggle dark and light mode"
      type="button"
    >
      {mode === 'dark' ? (
        <svg id="sun-icon" className="theme-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
          <path d="M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zm-9 5a1 1 0 0 0 1 1h2a1 1 0 0 0 0-2H4a1 1 0 0 0-1 1zm16 0a1 1 0 0 0 1 1h2a1 1 0 0 0 0-2h-2a1 1 0 0 0-1 1zM12 1a1 1 0 0 0-1 1v2a1 1 0 0 0 2 0V2a1 1 0 0 0-1-1zm0 16a1 1 0 0 0-1 1v2a1 1 0 0 0 2 0v-2a1 1 0 0 0-1-1zM5.99 4.58a1 1 0 0 0-1.42 1.42l1.06 1.06a1 1 0 1 0 1.42-1.42L5.99 4.58zm12.37 12.37a1 1 0 0 0-1.42 1.42l1.06 1.06a1 1 0 1 0 1.42-1.42l-1.06-1.06zm1.42-10.95a1 1 0 0 0-1.42-1.42l-1.06 1.06a1 1 0 0 0 1.42 1.42l1.06-1.06zM7.05 18.36a1 1 0 0 0-1.42-1.42l-1.06 1.06a1 1 0 1 0 1.42 1.42l1.06-1.06z" />
        </svg>
      ) : (
        <svg id="moon-icon" className="theme-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
          <path d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z" />
        </svg>
      )}
    </button>
  );
};

export default ThemeToggle;
export {};
