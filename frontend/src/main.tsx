import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConfigProvider, theme as antdTheme, App as AntdApp } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import App from "./App";
import "@/styles/global.css";

dayjs.locale("zh-cn");

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
      <ConfigProvider
        locale={zhCN}
        theme={{
          algorithm: antdTheme.darkAlgorithm,
          token: {
            colorPrimary: "#e34758",
            colorInfo: "#ed929c",
            colorLink: "#ed929c",
            colorBgBase: "#111214",
            colorBgContainer: "#18191c",
            colorBgElevated: "#1f2024",
            colorBorder: "#303136",
            colorBorderSecondary: "#26272c",
            colorText: "#eeedf0",
            colorTextSecondary: "#a4a4ad",
            colorTextTertiary: "#82838e",
            borderRadius: 8,
            fontSize: 13,
            controlHeight: 36,
            fontFamily:
              '"SF Pro Text", "Noto Sans SC", -apple-system, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
            fontFamilyCode: '"JetBrains Mono", "SF Mono", Menlo, Consolas, monospace',
          },
          components: {
            Layout: { siderBg: "#151618", headerBg: "#151618", bodyBg: "#111214" },
            Menu: {
              itemBg: "transparent",
              darkItemBg: "transparent",
              darkSubMenuItemBg: "transparent",
              itemColor: "#a4a4ad",
              itemHoverColor: "#eeedf0",
              itemSelectedColor: "#ff8290",
              itemHeight: 40,
              iconMarginInlineEnd: 12,
            },
            Table: {
              headerBg: "#1b1c20",
              headerColor: "#a4a4ad",
              rowHoverBg: "rgba(148, 163, 190, 0.05)",
              borderColor: "#26272c",
              headerSplitColor: "transparent",
            },
            Card: { paddingLG: 24 },
            Statistic: { contentFontSize: 26 },
            Segmented: { itemSelectedBg: "#34353c" },
            Tabs: { horizontalMargin: "0 0 4px 0" },
          },
        }}
      >
        <AntdApp>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </AntdApp>
      </ConfigProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
