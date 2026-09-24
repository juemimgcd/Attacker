import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider, theme as antdTheme } from "antd";
import { StyleProvider } from "@ant-design/cssinjs";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import App from "./App";
import { Toaster } from "@/components/ui/sonner";
import "@/styles/global.css";

dayjs.locale("zh-cn");

// Resolve the same CSS tokens used by shadcn for the retained Ant Design controls.
const cssToken = (name: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <StyleProvider layer>
        <ConfigProvider
          locale={zhCN}
          theme={{
            algorithm: antdTheme.defaultAlgorithm,
            token: {
              colorPrimary: cssToken("--primary"),
              colorInfo: cssToken("--primary"),
              colorLink: cssToken("--foreground"),
              colorSuccess: cssToken("--ok"),
              colorWarning: cssToken("--warn"),
              colorError: cssToken("--destructive"),
              colorBgBase: cssToken("--background"),
              colorBgContainer: cssToken("--card"),
              colorBgElevated: cssToken("--popover"),
              colorBorder: cssToken("--input"),
              colorBorderSecondary: cssToken("--border"),
              colorText: cssToken("--foreground"),
              colorTextSecondary: cssToken("--text-2"),
              colorTextTertiary: cssToken("--muted-foreground"),
              borderRadius: 8,
              fontSize: 13,
              controlHeight: 36,
              fontFamily:
                '"Geist Variable", -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
              fontFamilyCode: cssToken("--font-mono"),
            },
            components: {
              Table: {
                headerBg: cssToken("--muted"),
                headerColor: cssToken("--text-2"),
                rowHoverBg: cssToken("--muted"),
                borderColor: cssToken("--border"),
                headerSplitColor: "transparent",
                cellPaddingBlockMD: 15,
              },
              Card: { paddingLG: 24 },
              Tabs: { horizontalMargin: "0 0 16px 0" },
            },
          }}
        >
          <BrowserRouter>
            <App />
          </BrowserRouter>
          <Toaster theme="light" position="top-right" />
        </ConfigProvider>
      </StyleProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
