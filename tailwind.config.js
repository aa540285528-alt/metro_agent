const containerQueries = require('@tailwindcss/container-queries');
const forms = require('@tailwindcss/forms');

module.exports = {
  content: ['./src/metro_agent/static/preview.html'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        tertiary: '#3d4144',
        'on-secondary-fixed-variant': '#454748',
        'primary-fixed-dim': '#bcc3ff',
        'surface-bright': '#f9f9ff',
        'tertiary-container': '#55585b',
        'surface-variant': '#dce2f7',
        'inverse-on-surface': '#edf0ff',
        'surface-container-lowest': '#ffffff',
        'on-secondary-fixed': '#191c1d',
        'surface-dim': '#d3daef',
        'on-tertiary-fixed': '#191c1f',
        'on-primary-fixed': '#000d60',
        'outline-variant': '#c4c5da',
        'tertiary-fixed': '#e0e2e6',
        'primary-fixed': '#dfe0ff',
        'on-secondary': '#ffffff',
        error: '#ba1a1a',
        'inverse-surface': '#293040',
        'surface-tint': '#1f41ff',
        'error-container': '#ffdad6',
        'tertiary-fixed-dim': '#c4c7ca',
        'secondary-fixed-dim': '#c5c7c8',
        'on-surface': '#141b2b',
        background: '#f9f9ff',
        outline: '#747689',
        'surface-container': '#e9edff',
        'secondary-container': '#e1e3e4',
        'inverse-primary': '#bcc3ff',
        'surface-container-highest': '#dce2f7',
        'surface-container-high': '#e1e8fd',
        'on-tertiary-container': '#ccced2',
        'surface-container-low': '#f1f3ff',
        'on-surface-variant': '#444657',
        'on-tertiary-fixed-variant': '#44474a',
        'on-tertiary': '#ffffff',
        'on-secondary-container': '#626566',
        'on-primary': '#ffffff',
        primary: '#0033ff',
        'on-error-container': '#93000a',
        'primary-container': '#0033ff',
        'secondary-fixed': '#e1e3e4',
        'on-background': '#141b2b',
        surface: '#f9f9ff',
        secondary: '#5c5f60',
        'on-primary-container': '#c5caff',
        'on-error': '#ffffff',
        'on-primary-fixed-variant': '#0029d3'
      },
      borderRadius: {
        DEFAULT: '0.25rem',
        lg: '0.5rem',
        xl: '0.75rem',
        full: '9999px'
      },
      spacing: {
        gutter: '24px',
        'stack-sm': '8px',
        'chat-max': '800px',
        'stack-md': '16px',
        'margin-mobile': '16px',
        'stack-lg': '32px',
        'container-max': '1200px',
        'sidebar-expanded': '264px',
        'sidebar-collapsed': '64px'
      },
      fontFamily: {
        'headline-lg-mobile': ['ui-sans-serif', 'system-ui', 'sans-serif'],
        'headline-lg': ['ui-sans-serif', 'system-ui', 'sans-serif'],
        'label-caps': ['ui-sans-serif', 'system-ui', 'sans-serif'],
        'body-md': ['ui-sans-serif', 'system-ui', 'sans-serif'],
        'code-sm': ['ui-monospace', 'SFMono-Regular', 'Consolas', 'monospace'],
        'body-lg': ['ui-sans-serif', 'system-ui', 'sans-serif'],
        'headline-md': ['ui-sans-serif', 'system-ui', 'sans-serif']
      },
      fontSize: {
        'headline-lg-mobile': ['24px', { lineHeight: '32px', letterSpacing: '0', fontWeight: '600' }],
        'headline-lg': ['32px', { lineHeight: '40px', letterSpacing: '0', fontWeight: '600' }],
        'label-caps': ['12px', { lineHeight: '16px', letterSpacing: '0', fontWeight: '600' }],
        'body-md': ['14px', { lineHeight: '22px', fontWeight: '400' }],
        'code-sm': ['13px', { lineHeight: '20px', fontWeight: '400' }],
        'body-lg': ['16px', { lineHeight: '26px', fontWeight: '400' }],
        'headline-md': ['20px', { lineHeight: '28px', letterSpacing: '0', fontWeight: '500' }]
      }
    }
  },
  plugins: [forms, containerQueries]
};
