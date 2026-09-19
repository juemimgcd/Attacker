import { create } from "zustand";
import { persist } from "zustand/middleware";

/** 服务级 API Key 仅保存在浏览器 localStorage，与现有静态控制台行为一致。 */
interface AuthState {
  apiKey: string | null;
  setApiKey: (key: string | null) => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      apiKey: null,
      setApiKey: (key) => set({ apiKey: key?.trim() || null }),
    }),
    { name: "attacker.auth" },
  ),
);
