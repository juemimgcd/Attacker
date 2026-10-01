import { useSyncExternalStore } from "react";

const MOBILE_QUERY = "(max-width: 767px)";
const subscribe = (callback: () => void) => {
  const query = window.matchMedia(MOBILE_QUERY);
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
};
const getSnapshot = () => window.matchMedia(MOBILE_QUERY).matches;

export function useIsMobile() {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
