// study_1 README 展示图：打开前端 → 发消息 → 截聊天界面 + 审批面板
// 用法: node tools/shot.mjs
// 前置: 后端已起 (uvicorn app:app --port 8005)，src/mcp_servers 可连
import { chromium } from 'playwright';
import { mkdir } from 'node:fs/promises';

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8005';
const OUT = process.env.OUT_DIR || 'docs/assets/screenshots';

await mkdir(OUT, { recursive: true });

const browser = await chromium.launch({
  executablePath: '/Users/rain/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell',
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

page.on('console', (m) => { if (m.type() === 'error') console.log('[console.error]', m.text().slice(0, 200)); });
page.on('pageerror', (e) => console.log('[pageerror]', String(e).slice(0, 200)));

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.waitForSelector('textarea', { timeout: 15000 });

// 发消息并等回复完成（发送按钮 disabled 恢复 = 流结束）
async function sendMessage(text) {
  await page.fill('textarea', text);
  await page.click('button[type="button"]:has-text("发送"), button:has-text("发送")').catch(async () => {
    // 若无"发送"按钮，找发消息图标按钮（最后一个非 disabled）
    await page.evaluate((t) => {
      const ta = document.querySelector('textarea');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(ta, t);
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }, text);
    await page.waitForTimeout(300);
    await page.keyboard.press('Enter');
  });
  // 等按钮恢复可用（流式结束）：轮询 textarea 是否可输入 & 无 loading
  await page.waitForFunction(
    () => {
      const btn = document.querySelector('button:not([disabled])');
      const loading = document.querySelector('.animate-spin, [class*="loading"], [class*="typing"]');
      return btn && !loading && !document.querySelector('.opacity-50.pointer-events-none');
    },
    null,
    { timeout: 240000 },
  );
  await page.waitForTimeout(1500);
}

// ---- 图 1: 售前对话 ----
await sendMessage('羽绒服用什么面料比较好？给我推荐几款。');
await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
await page.waitForTimeout(800);
await page.screenshot({ path: `${OUT}/chat-inquiry.png` });
console.log('saved chat-inquiry.png');

// ---- 图 2: 下单挂起（HITL 审批）----
await sendMessage('我要 T400 黑色 1000 米下单，电话 13800000000 地址 杭州钱塘路');
await page.screenshot({ path: `${OUT}/chat-order.png` });
console.log('saved chat-order.png');

await browser.close();
console.log('done');