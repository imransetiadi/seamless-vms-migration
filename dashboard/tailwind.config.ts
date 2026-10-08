import type { Config } from 'tailwindcss';
import defaultTheme from 'tailwindcss/defaultTheme';

// Every colour is a CSS variable holding "R G B" channels (src/index.css), so Tailwind's
// opacity modifiers keep working (`bg-accent/10`) and both themes share one class vocabulary
// (`bg-background`, `text-muted-foreground`, … — SDD §16).
const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    screens: {
      // Systematic breakpoints from the design system: 375 / 768 / 1024 / 1440.
      sm: '640px',
      md: '768px',
      lg: '1024px',
      xl: '1280px',
      '2xl': '1440px',
    },
    extend: {
      colors: {
        background: token('background'),
        foreground: token('foreground'),
        card: { DEFAULT: token('card'), foreground: token('card-foreground') },
        muted: { DEFAULT: token('muted'), foreground: token('muted-foreground') },
        border: token('border'),
        input: token('input'),
        primary: { DEFAULT: token('primary'), foreground: token('primary-foreground') },
        accent: { DEFAULT: token('accent'), foreground: token('accent-foreground') },
        destructive: { DEFAULT: token('destructive'), foreground: token('destructive-foreground') },
        ring: token('ring'),
        status: {
          neutral: token('status-neutral'),
          info: token('status-info'),
          progress: token('status-progress'),
          warning: token('status-warning'),
          success: token('status-success'),
          danger: token('status-danger'),
        },
        chart: {
          1: token('chart-1'),
          2: token('chart-2'),
          reference: token('chart-reference'),
          grid: token('chart-grid'),
        },
      },
      fontFamily: {
        sans: ['"Fira Sans"', ...defaultTheme.fontFamily.sans],
        mono: ['"Fira Code"', ...defaultTheme.fontFamily.mono],
      },
      transitionDuration: {
        DEFAULT: '200ms',
      },
      zIndex: {
        nav: '20',
        overlay: '40',
        modal: '50',
        toast: '60',
      },
    },
  },
  plugins: [],
} satisfies Config;
