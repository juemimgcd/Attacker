import { useState } from "react";
import { Table, Typography } from "antd";
import { CircleAlert, PackageOpen, RefreshCw } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { listEquipment } from "@/api/client";
import PageHeader from "@/components/PageHeader";
import JsonViewer from "@/components/JsonViewer";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { EquipmentPackage, PackageType } from "@/types/api";
import type { ColumnsType } from "antd/es/table";

const tabs: { key: PackageType; label: string }[] = [
  { key: "provider", label: "Provider" },
  { key: "skill", label: "Skill" },
  { key: "case_pack", label: "Case Pack" },
  { key: "benchmark", label: "Benchmark" },
];

function manifestStrings(record: EquipmentPackage, key: "capabilities" | "tags") {
  const manifest = record.manifest;
  const fallback =
    manifest && typeof manifest === "object" && !Array.isArray(manifest)
      ? (manifest as Record<string, unknown>)[key]
      : undefined;
  const values = record[key] ?? fallback;
  return Array.isArray(values)
    ? values.filter((value): value is string => typeof value === "string" && value.trim().length > 0)
    : [];
}

/** 装备目录：Manifest、JSON Schema、兼容性与 checksum 校验后的本地包。 */
export default function EquipmentPage() {
  const [activeTab, setActiveTab] = useState<PackageType>("provider");

  const packagesQuery = useQuery({
    queryKey: ["equipment", activeTab],
    queryFn: () => listEquipment(activeTab),
  });

  const columns: ColumnsType<EquipmentPackage> = [
    {
      title: "包 ID",
      dataIndex: "package_id",
      width: 270,
      render: (value: string, record) => (
        <div className="flex min-w-0 flex-col gap-1">
          <span className="mono max-w-60 truncate" title={value}>{value}</span>
          {record.name && record.name !== value && (
            <span className="max-w-60 truncate text-xs text-muted-foreground" title={record.name}>
              {record.name}
            </span>
          )}
        </div>
      ),
    },
    { title: "版本", dataIndex: "version", width: 100 },
    {
      title: "状态",
      width: 160,
      render: (_, record) => (
        <div className="flex flex-wrap gap-1.5">
          <Badge variant={record.enabled ? "secondary" : "outline"}>
            {record.enabled ? "已启用" : "已禁用"}
          </Badge>
          {record.validation_status && (
            <Badge
              variant={
                record.validation_status === "valid" ||
                record.validation_status === "validated"
                  ? "outline"
                  : "destructive"
              }
            >
              {record.validation_status}
            </Badge>
          )}
        </div>
      ),
    },
    {
      title: "能力 / 标签",
      render: (_, record) => {
        const capabilities = [...new Set(manifestStrings(record, "capabilities"))];
        const tags = [...new Set(manifestStrings(record, "tags"))];

        return capabilities.length || tags.length ? (
          <div className="flex flex-wrap gap-1.5">
            {capabilities.map((capability) => (
              <Badge key={`capability:${capability}`} variant="outline" title={capability}>
                <span className="max-w-40 truncate">{capability}</span>
              </Badge>
            ))}
            {tags.map((tag) => (
              <Badge key={`tag:${tag}`} variant="secondary" title={tag}>
                <span className="max-w-40 truncate">{tag}</span>
              </Badge>
            ))}
          </div>
        ) : <span className="text-muted-foreground">—</span>;
      },
    },
    {
      title: "Checksum",
      dataIndex: "checksum",
      width: 120,
      render: (value: string | undefined) =>
        value ? (
          <Typography.Text
            className="mono"
            style={{ fontSize: 11 }}
            copyable={{ text: value, tooltips: ["复制", "已复制"] }}
          >
            {value.slice(0, 10)}…
          </Typography.Text>
        ) : (
          "—"
        ),
    },
    {
      title: "详情",
      width: 80,
      render: (_, record) => <JsonViewer data={record} title={record.package_id} />,
    },
  ];

  return (
    <>
      <PageHeader
        eyebrow="Equipment Catalog"
        title="装备目录"
        desc="浏览可用的评测能力、用例包与基准，检查版本和校验状态。"
        extra={
          <Button variant="outline" onClick={() => packagesQuery.refetch()} disabled={packagesQuery.isFetching}>
            {packagesQuery.isFetching ? <Spinner data-icon="inline-start" /> : <RefreshCw data-icon="inline-start" />}
            刷新目录
          </Button>
        }
      />

      {packagesQuery.isError && (
        <Alert variant="destructive" className="mb-4">
          <CircleAlert />
          <AlertTitle>无法加载装备目录</AlertTitle>
          <AlertDescription>{(packagesQuery.error as Error).message}</AlertDescription>
        </Alert>
      )}

      <Tabs value={activeTab} onValueChange={(key) => setActiveTab(key as PackageType)} className="gap-5">
        <div className="max-w-full overflow-x-auto pb-1">
          <TabsList variant="line" aria-label="装备包类型">
            {tabs.map((tab) => <TabsTrigger key={tab.key} value={tab.key}>{tab.label}</TabsTrigger>)}
          </TabsList>
        </div>
        {tabs.map((tab) => (
          <TabsContent key={tab.key} value={tab.key}>
            <Table
              className="table-panel"
              rowKey="package_id"
              size="middle"
              columns={columns}
              dataSource={packagesQuery.data ?? []}
              loading={packagesQuery.isLoading}
              scroll={{ x: 850 }}
              pagination={{ pageSize: 15, showSizeChanger: false }}
              locale={{
                emptyText: (
                  <Empty className="py-12">
                    <EmptyHeader>
                      <EmptyMedia variant="icon"><PackageOpen /></EmptyMedia>
                      <EmptyTitle>{packagesQuery.isError ? "装备数据暂不可用" : "该目录下没有装备包"}</EmptyTitle>
                      <EmptyDescription>
                        {packagesQuery.isError
                          ? "检查后端连接或 API Key 后刷新目录。"
                          : "切换其他目录，查看可用的评测能力与用例包。"}
                      </EmptyDescription>
                    </EmptyHeader>
                  </Empty>
                ),
              }}
            />
          </TabsContent>
        ))}
      </Tabs>
    </>
  );
}
