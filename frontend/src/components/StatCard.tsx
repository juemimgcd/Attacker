import type { CSSProperties, ReactNode } from "react";
import { Skeleton } from "@/components/ui/skeleton";

export default function StatCard({
  label,
  value,
  suffix,
  accent,
  valueColor,
  hint,
  loading = false,
  icon,
}: {
  label: string;
  value: number | string;
  suffix?: string;
  accent: string;
  valueColor?: string;
  hint?: string;
  loading?: boolean;
  icon?: ReactNode;
}) {
  return (
    <div
      className="stat-card"
      style={{ "--stat-accent": accent } as CSSProperties}
      aria-busy={loading}
    >
      <div className="stat-label">
        <span>{label}</span>
        {icon ?? <span className="stat-dot" />}
      </div>
      {loading ? (
        <Skeleton className="mt-4 mb-2 h-10 w-20" aria-label="正在加载" />
      ) : (
        <div className="stat-value" style={{ color: valueColor }}>
          {value}
          {suffix && <span className="stat-suffix">{suffix}</span>}
        </div>
      )}
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}
