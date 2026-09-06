/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // DeepSeek 蓝（提取自 DSH 界面 token：deepseek-50..900 + 蓝调中性层）
        brand: {
          50: '#edf3fe',
          100: '#e4edfd',
          200: '#d3e2ff',
          300: '#b7c8fe',
          400: '#679efe',
          500: '#5686fe',
          600: '#4176e6',
          700: '#3a62c7',
          800: '#34415b',
          900: '#283142',
          950: '#1c2333',
        },
      },
      fontFamily: {
        display: ['"Space Grotesk"', '"DM Sans"', 'system-ui', 'sans-serif'],
        sans: ['"DM Sans"', 'system-ui', '-apple-system', 'sans-serif'],
      },
      borderRadius: {
        // 圆角体系：整体比默认再圆一小档（玻璃语言）
        // lg 8→10px（小按钮/徽章）、xl 12→14px（次级卡/控件）、2xl 16→20px（面板/气泡）、3xl 24→28px（登录卡）
        lg: '0.625rem',
        xl: '0.875rem',
        '2xl': '1.25rem',
        '3xl': '1.75rem',
      },
      boxShadow: {
        // DSH 式柔和阴影：黑底 + 中距模糊，无生硬重影
        soft: '0 1px 2px rgba(0,0,0,0.35), 0 8px 24px -6px rgba(0,0,0,0.45)',
        lift: '0 2px 4px rgba(0,0,0,0.35), 0 18px 44px -8px rgba(0,0,0,0.55)',
        glow: '0 0 0 1px rgba(86,134,254,0.18), 0 8px 32px -6px rgba(86,134,254,0.35)',
        // 玻璃面板立体感：inset 顶部高光（受光边）+ 分层柔影
        panel: 'inset 0 1px 0 rgba(255,255,255,0.10), inset 0 0 0 0.5px rgba(255,255,255,0.03), 0 2px 6px rgba(0,0,0,0.22), 0 18px 44px -8px rgba(0,0,0,0.5)',
        'panel-sm': 'inset 0 1px 0 rgba(255,255,255,0.08), 0 1px 2px rgba(0,0,0,0.28), 0 8px 24px -6px rgba(0,0,0,0.45)',
      },
      animation: {
        'fade-up': 'fadeUp 0.4s ease-out both',
        'pulse-dot': 'pulseDot 1.2s ease-in-out infinite',
      },
      keyframes: {
        fadeUp: {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        pulseDot: {
          '0%, 100%': { opacity: '0.3', transform: 'translateY(0)' },
          '50%': { opacity: '1', transform: 'translateY(-2px)' },
        },
      },
    },
  },
  plugins: [],
}
