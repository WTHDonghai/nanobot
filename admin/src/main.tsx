import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';

// Apply saved theme before React renders to prevent flash of wrong theme
const savedTheme = localStorage.getItem('theme');
document.documentElement.setAttribute('data-theme', savedTheme === 'light' ? 'light' : 'dark');


ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
