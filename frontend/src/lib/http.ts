import axios, { AxiosError } from "axios";
import { useAuthStore } from "@/lib/auth";

/** 与后端约定：业务 API 通过 X-API-Key 头保护；本地未配置密钥时后端放行。 */
export const http = axios.create({
  baseURL: "/",
  timeout: 60_000,
});

http.interceptors.request.use((config) => {
  const apiKey = useAuthStore.getState().apiKey;
  if (apiKey) {
    config.headers["X-API-Key"] = apiKey;
  }
  return config;
});

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** 把 FastAPI 的 detail 错误统一成可读信息。 */
export function toApiError(error: unknown): ApiError {
  if (error instanceof AxiosError) {
    const status = error.response?.status ?? 0;
    const data = error.response?.data;
    let detail = error.message;
    if (typeof data?.detail === "string") {
      detail = data.detail;
    } else if (Array.isArray(data?.detail)) {
      detail = data.detail
        .map((item: { loc?: unknown[]; msg?: string }) =>
          `${(item.loc ?? []).join(".")}: ${item.msg ?? "校验失败"}`,
        )
        .join("; ");
    }
    if (status === 401) {
      detail = "API Key 无效或缺失，请在右上角设置。";
    }
    return new ApiError(status, detail);
  }
  if (error instanceof Error) {
    return new ApiError(0, error.message);
  }
  return new ApiError(0, "未知错误");
}
