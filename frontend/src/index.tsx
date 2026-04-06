/**
 * frontend/src/index.tsx
 * ============================================================
 * PURPOSE:
 *   React application entry point. Mounts the root <App> component
 *   into the #root DOM node created by public/index.html.
 *
 * React.StrictMode:
 *   Enabled in development only (CRA strips it in production builds).
 *   Causes double-invocation of effects and renders to surface bugs.
 *   App.tsx uses healthCheckedRef to suppress the duplicate health
 *   check that StrictMode would otherwise trigger.
 * ============================================================
 */

import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './App';

const rootElement = document.getElementById('root');

if (!rootElement) {
  throw new Error(
    'Root element #root not found. Check public/index.html has <div id="root"></div>.',
  );
}

const root = ReactDOM.createRoot(rootElement);

root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
