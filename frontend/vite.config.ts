import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";
import type { IncomingMessage } from "node:http";

// 开发服务器把 API 代理到本地 FastAPI，避免 CORS；生产构建由 FastAPI 同源静态挂载。
// bypass：浏览器页面导航（Accept: text/html）不代理，交给 SPA 路由渲染。
const proxyTarget = "http://127.0.0.1:8000";

function apiProxy(path: string): Record<string, ProxyOptions> {
  return {
    [path]: {
      target: proxyTarget,
      bypass(req: IncomingMessage) {
        const accept = req.headers.accept ?? "";
        if (accept.includes("text/html")) {
          return "/index.html";
        }
        return undefined;
      },
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      ...apiProxy("/runs"),
      ...apiProxy("/jobs"),
      ...apiProxy("/equipment"),
      ...apiProxy("/tests"),
      ...apiProxy("/health"),
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
});
