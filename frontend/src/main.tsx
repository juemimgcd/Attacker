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
            colorPrimary: "#d7263d",
            colorInfo: "#e05c6e",
            colorLink: "#e05c6e",
            colorBgBase: "#08090d",
            colorBgContainer: "#10141b",
            colorBgElevated: "#151a23",
            colorBorder: "#222a39",
            colorBorderSecondary: "#1a2130",
            colorText: "#edeff3",
            colorTextSecondary: "#99a3b5",
            colorTextTertiary: "#5e6879",
            borderRadius: 10,
            fontSize: 13,
            controlHeight: 34,
          },
          components: {
            Layout: { siderBg: "#0c0e13", headerBg: "#0c0e13", bodyBg: "#08090d" },
            Menu: {
              itemBg: "transparent",
              darkItemBg: "transparent",
              darkSubMenuItemBg: "transparent",
              itemColor: "#99a3b5",
              itemHoverColor: "#edeff3",
              itemSelectedColor: "#ff8291",
              itemHeight: 40,
              iconMarginInlineEnd: 12,
            },
            Table: {
              headerBg: "#0d1016",
              headerColor: "#99a3b5",
              rowHoverBg: "#131926",
              borderColor: "#1a2130",
              headerSplitColor: "transparent",
            },
            Card: { paddingLG: 20 },
            Statistic: { contentFontSize: 26 },
            Segmented: { itemSelectedBg: "#1f2735" },
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
