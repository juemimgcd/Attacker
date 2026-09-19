/** 品牌统计卡：左侧色条 + 大数字，accent 取 CSS 变量。 */
export default function StatCard({
  label,
  value,
  suffix,
  accent,
  valueColor,
}: {
  label: string;
  value: number | string;
  suffix?: string;
  accent: string;
  valueColor?: string;
}) {
  return (
    <div
      className="stat-card"
      style={{ ["--accent" as string]: accent, padding: "16px 18px" }}
    >
      <div className="stat-label">{label}</div>
      <div
        style={{
          fontSize: 26,
          fontWeight: 700,
          fontVariantNumeric: "tabular-nums",
          color: valueColor ?? "var(--text-1)",
          lineHeight: 1.3,
        }}
      >
        {value}
        {suffix && (
          <span style={{ fontSize: 13, fontWeight: 500, marginLeft: 2 }}>{suffix}</span>
        )}
      </div>
    </div>
  );
}
