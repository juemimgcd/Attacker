import type { CSSProperties } from "react";

/** 共享指标组件：颜色用于状态标记，数字保持清晰、稳定。 */
export default function StatCard({
  label,
  value,
  suffix,
  accent,
  valueColor,
  hint,
  loading = false,
}: {
  label: string;
  value: number | string;
  suffix?: string;
  accent: string;
  valueColor?: string;
  hint?: string;
  loading?: boolean;
}) {
  return (
    <div className="stat-card" style={{ "--accent": accent } as CSSProperties} aria-busy={loading}>
      <div className="stat-label"><span className="stat-dot" />{label}</div>
      <div className={`stat-value ${loading ? "stat-loading" : ""}`} style={{ color: valueColor }}>
        {loading ? <span aria-label="正在加载">—</span> : value}
        {!loading && suffix && <span className="stat-suffix">{suffix}</span>}
      </div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}
