import { useState } from "react";
import { Card, Table, Tabs, Tag, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { listEquipment } from "@/api/client";
import PageHeader from "@/components/PageHeader";
import JsonViewer from "@/components/JsonViewer";
import type { EquipmentPackage, PackageType } from "@/types/api";
import type { ColumnsType } from "antd/es/table";

const tabs: { key: PackageType; label: string }[] = [
  { key: "provider", label: "Provider" },
  { key: "skill", label: "Skill" },
  { key: "case_pack", label: "Case Pack" },
  { key: "benchmark", label: "Benchmark" },
];

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
      render: (value: string) => <span className="mono">{value}</span>,
    },
    { title: "版本", dataIndex: "version", width: 100 },
    {
      title: "状态",
      width: 160,
      render: (_, record) => (
        <>
          <Tag color={record.enabled ? "success" : "default"}>
            {record.enabled ? "已启用" : "已禁用"}
          </Tag>
          {record.validation_status && (
            <Tag
              color={
                record.validation_status === "valid" ||
                record.validation_status === "validated"
                  ? "blue"
                  : "orange"
              }
            >
              {record.validation_status}
            </Tag>
          )}
        </>
      ),
    },
    {
      title: "能力 / 标签",
      ellipsis: true,
      render: (_, record) => (
        <>
          {(record.capabilities ?? []).map((cap) => (
            <Tag key={cap}>{cap}</Tag>
          ))}
          {(record.tags ?? []).map((tag) => (
            <Tag key={tag} color="geekblue">
              {tag}
            </Tag>
          ))}
        </>
      ),
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
        desc="本地目录加载的 Provider、Skill、Case Pack 与 Benchmark，均通过 Manifest、JSON Schema、兼容性与 checksum 校验。"
      />

      <Card className="panel">
        <Tabs
          activeKey={activeTab}
          onChange={(key) => setActiveTab(key as PackageType)}
          items={tabs.map((tab) => ({
            key: tab.key,
            label: tab.label,
            children: (
              <Table
                rowKey="package_id"
                size="small"
                columns={columns}
                dataSource={packagesQuery.data ?? []}
                loading={packagesQuery.isLoading}
                pagination={{ pageSize: 15, showSizeChanger: false }}
                locale={{
                  emptyText: packagesQuery.isError
                    ? `加载失败：${(packagesQuery.error as Error)?.message}`
                    : "该目录下没有装备包",
                }}
              />
            ),
          }))}
        />
      </Card>
    </>
  );
}
