import { Space, Typography } from "antd";
import type { ReactNode } from "react";

/** 统一页头：红色眉题 + 大标题 + 描述 + 右侧操作区。 */
export default function PageHeader({
  eyebrow,
  title,
  desc,
  extra,
}: {
  eyebrow: string;
  title: string;
  desc?: string;
  extra?: ReactNode;
}) {
  return (
    <div className="page-header" style={{ display: "flex", justifyContent: "space-between" }}>
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1 className="page-title">{title}</h1>
        {desc && <div className="page-desc">{desc}</div>}
      </div>
      {extra && <Space align="start">{extra}</Space>}
    </div>
  );
}

export function DividerLabel({ children }: { children: ReactNode }) {
  return <Typography.Text className="divider-label">{children}</Typography.Text>;
}
