import React, { useEffect, useState } from 'react'
import ReactDOM from 'react-dom/client'
import { HashRouter, Route, Routes } from 'react-router-dom'
import { initializeIcons } from '@fluentui/react'

import Chat from './pages/chat/Chat'
import Layout from './pages/layout/Layout'
import NoPage from './pages/NoPage'
import { AppStateProvider } from './state/AppProvider'
import { ColorModeProvider } from './theme/useColorMode';

import './index.css'

initializeIcons("https://res.cdn.office.net/files/fabric-cdn-prod_20241209.001/assets/icons/")

function useFrontendSettings() {
  const [settings, setSettings] = useState<any>(null);

  useEffect(() => {
    fetch('/frontend_settings')
      .then(res => res.json())
      .then(data => {
        setSettings(data);
        if (data?.ui?.title) {
          document.title = data.ui.title;
        }
        if (data?.ui?.chat_logo) {
          let favicon = document.querySelector("link[rel='icon']");
          if (!favicon) {
            favicon = document.createElement('link');
            favicon.setAttribute('rel', 'icon');
            document.head.appendChild(favicon);
          }
          favicon.setAttribute('href', data.ui.chat_logo);
        }
      });
  }, []);

  return settings;
}

export default function App() {
  useFrontendSettings();

  return (
    <ColorModeProvider>
      <AppStateProvider>
        <HashRouter>
          <Routes>
            <Route path="/" element={<Layout />}>
              <Route index element={<Chat />} />
              <Route path="*" element={<NoPage />} />
            </Route>
          </Routes>
        </HashRouter>
      </AppStateProvider>
    </ColorModeProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
