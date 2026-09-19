import { useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Button,
  Input,
  Layout,
  Menu,
  Modal,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  AuditOutlined,
  DashboardOutlined,
  DeploymentUnitOutlined,
  KeyOutlined,
  PlayCircleOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { getHealth, listJobs } from "@/api/client";
import { useAuthStore } from "@/lib/auth";

const { Sider, Header, Content } = Layout;

const menuItems = [
  { key: "/dashboard", icon: <DashboardOutlined />, label: "总览" },
  { key: "/jobs", icon: <UnorderedListOutlined />, label: "任务队列" },
  { key: "/runs/new", icon: <PlayCircleOutlined />, label: "新建评测" },
  { key: "/approvals", icon: <AuditOutlined />, label: "审批中心" },
  { key: "/equipment", icon: <DeploymentUnitOutlined />, label: "装备目录" },
];

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { apiKey, setApiKey } = useAuthStore();
  const [keyModalOpen, setKeyModalOpen] = useState(false);
  const [draftKey, setDraftKey] = useState("");

  const healthQuery = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 15_000,
  });

  const activeCountQuery = useQuery({
    queryKey: ["jobs", "active-count"],
    queryFn: () => listJobs(undefined, 500),
    refetchInterval: 10_000,
    select: (jobs) =>
      jobs.filter((j) => ["queued", "leased", "running", "retry_wait"].includes(j.status))
        .length,
  });

  const healthy =
    healthQuery.data?.status === "ready" || healthQuery.data?.status === "ok";
  const selectedKey =
    menuItems.find((item) => location.pathname.startsWith(item.key))?.key ??
    (location.pathname.startsWith("/runs/") ? "/jobs" : "/dashboard");

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider width={216} theme="dark" className="sider">
        <div className="brand">
          <div className="brand-logo">
            <img src="/logo.png" alt="Attacker logo" />
          </div>
          <div className="brand-wordmark">
            <div className="title">ATTACKER</div>
            <div className="subtitle">Agent Security</div>
          </div>
        </div>
        <Menu
          theme="dark"
          mode="inline"
          className="sider-menu"
          selectedKeys={[selectedKey]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
        <div className="sider-footer">
          <div className="sider-footer-title">Authorization Notice</div>
          仅用于已获授权目标的隔离评测环境
        </div>
      </Sider>

      <Layout>
        <Header className="app-header">
          <Space size={14}>
            <Space size={8}>
              {healthQuery.isError ? (
                <span className="pulse-dot red" />
              ) : healthy ? (
                <span className="pulse-dot" />
              ) : (
                <span className="pulse-dot" style={{ background: "#5e6879" }} />
              )}
              <span style={{ color: "#99a3b5", fontSize: 12 }}>
                {healthQuery.isError
                  ? "后端离线"
                  : healthQuery.isLoading
                    ? "连接中…"
                    : healthy
                      ? `控制面在线 · ${healthQuery.data?.environment ?? ""}`
                      : "依赖未就绪"}
              </span>
            </Space>
            {typeof activeCountQuery.data === "number" && activeCountQuery.data > 0 && (
              <Tag color="red" bordered={false} style={{ marginInlineEnd: 0 }}>
                {activeCountQuery.data} 个任务执行中
              </Tag>
            )}
          </Space>
          <Tooltip title={apiKey ? "已配置 API Key" : "未配置 API Key（本地开发可留空）"}>
            <Button
              size="small"
              icon={<KeyOutlined />}
              type={apiKey ? "default" : "dashed"}
              onClick={() => {
                setDraftKey(apiKey ?? "");
                setKeyModalOpen(true);
              }}
            >
              {apiKey ? "API Key 已配置" : "设置 API Key"}
            </Button>
          </Tooltip>
        </Header>

        <Content className="app-content">
          <div className="content-inner">
            <Outlet />
          </div>
        </Content>
      </Layout>

      <Modal
        title="服务级 API Key"
        open={keyModalOpen}
        onCancel={() => setKeyModalOpen(false)}
        onOk={() => {
          setApiKey(draftKey);
          setKeyModalOpen(false);
        }}
        okText="保存"
        cancelText="取消"
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          Key 仅保存在浏览器 localStorage，通过 <code>X-API-Key</code> 头发送。留空并保存可清除。
        </Typography.Paragraph>
        <Input.Password
          value={draftKey}
          onChange={(event) => setDraftKey(event.target.value)}
          placeholder="ATTACKER_API_KEY"
          autoComplete="off"
        />
      </Modal>
    </Layout>
  );
}
