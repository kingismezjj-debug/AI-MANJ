import type { Metadata } from "next";
import "@fontsource/noto-sans-sc/400.css";
import "@fontsource/noto-sans-sc/700.css";
import "@fontsource/noto-serif-sc/600.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "帧川",
  description: "小说进，漫剧出。竖屏 AI 短剧自动生产线。",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <div className="shell">
          <header className="topbar">
            <a className="brand" href="/">
              <div className="seal">帧川</div>
              <div>
                <p className="wordmark">帧川</p>
                <p className="tagline">小说进，漫剧出</p>
              </div>
            </a>
            <nav className="nav">
              <a href="/">项目</a>
              <a href="/settings">模型设置</a>
              <a href="/admin">积分</a>
            </nav>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
