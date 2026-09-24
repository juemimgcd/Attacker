import type { ReactNode } from "react";
import { Separator } from "@/components/ui/separator";

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
    <div className="page-header">
      <div className="page-heading">
        <div className="eyebrow">{eyebrow}</div>
        <h1 className="page-title">{title}</h1>
        {desc && <p className="page-desc">{desc}</p>}
      </div>
      {extra && <div className="page-actions">{extra}</div>}
    </div>
  );
}

export function DividerLabel({ children }: { children: ReactNode }) {
  return (
    <div className="divider-label">
      <span>{children}</span>
      <Separator className="flex-1" />
    </div>
  );
}
