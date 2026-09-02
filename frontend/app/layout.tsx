import type { Metadata } from 'next';
import { Inter, JetBrains_Mono } from 'next/font/google';
import type { ReactNode } from 'react';

// globals.css must load before any client component pulls in the editor's antd
// stylesheet, so our tokens win over antd's reset.
import './globals.css';

import { ThemeBoot } from '../components/shell/ThemeBoot';

const inter = Inter({
  subsets: ['latin'],
  variable: '--font-inter',
  display: 'swap',
});

const mono = JetBrains_Mono({
  subsets: ['latin'],
  variable: '--font-mono-jetbrains',
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'JDM Studio',
  description: 'Author, test and version GoRules JDM decision graphs with an AI assistant.',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`h-full ${inter.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>
        {/*
          Applies the stored theme before first paint. Without this the page
          flashes light before hydration sets data-theme.
        */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('jdm-studio-theme');if(t!=='light'&&t!=='dark'){t=window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}document.documentElement.setAttribute('data-theme',t);}catch(e){}})();`,
          }}
        />
      </head>
      <body className="h-full overflow-hidden">
        <ThemeBoot />
        {children}
      </body>
    </html>
  );
}
