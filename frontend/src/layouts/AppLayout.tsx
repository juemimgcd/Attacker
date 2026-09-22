import { Suspense, useState } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Button, Drawer, Grid, Input, Layout, Menu, Modal, Spin, Tooltip, Typography } from "antd";
import {
  ArrowRightOutlined,
  AuditOutlined,
  DashboardOutlined,
  DeploymentUnitOutlined,
  KeyOutlined,
  MenuOutlined,
  PlusOutlined,
  SafetyCertificateOutlined,
  UnorderedListOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { getHealth, listJobs } from "@/api/client";
import { useAuthStore } from "@/lib/auth";

const { Sider, Header, Content } = Layout;
const menuItems = [
  { key: "/dashboard", icon: <DashboardOutlined />, label: "总览" },
  { key: "/jobs", icon: <UnorderedListOutlined />, label: "任务队列" },
  { key: "/approvals", icon: <AuditOutlined />, label: "审批中心" },
  { key: "/equipment", icon: <DeploymentUnitOutlined />, label: "装备目录" },
];

export default function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const screens = Grid.useBreakpoint();
  const { apiKey, setApiKey } = useAuthStore();
  const [keyModalOpen, setKeyModalOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [draftKey, setDraftKey] = useState("");

  const healthQuery = useQuery({
    queryKey: ["health"], queryFn: getHealth, refetchInterval: 15_000,
  });
  const activeCountQuery = useQuery({
    queryKey: ["jobs", "active-count"],
    queryFn: () => listJobs(undefined, 500),
    refetchInterval: 10_000,
    select: (jobs) => jobs.filter((j) => ["queued", "leased", "running", "retry_wait"].includes(j.status)).length,
  });

  const healthy = healthQuery.data?.status === "ready" || healthQuery.data?.status === "ok";
  const newRun = location.pathname === "/runs/new";
  const selectedKey = menuItems.find((item) => location.pathname.startsWith(item.key))?.key ??
    (location.pathname.startsWith("/runs/") ? "/jobs" : "/dashboard");
  const pageName = newRun ? "新建评测" : location.pathname.startsWith("/runs/") ? "评测详情" :
    menuItems.find((item) => item.key === selectedKey)?.label;
  const statusText = healthQuery.isError ? "控制面离线" : healthQuery.isLoading ? "正在连接" : healthy ? "控制面在线" : "依赖未就绪";
  const statusClass = healthQuery.isError ? "red" : healthy ? "" : "muted";

  const navigation = (
    <div className="navigation-shell">
      <Link className="brand" to="/dashboard" onClick={() => setNavOpen(false)} aria-label="Attacker 总览">
        <div className="brand-logo"><img src="/logo.png" alt="" /></div>
        <div className="brand-wordmark"><div className="title">ATTACKER</div><div className="subtitle">Agent Security</div></div>
        <span className="brand-edition">01</span>
      </Link>
      <div className="sider-create">
        <Button type="primary" icon={<PlusOutlined />} block onClick={() => { navigate("/runs/new"); setNavOpen(false); }}>
          新建评测 <ArrowRightOutlined className="create-arrow" />
        </Button>
      </div>
      <div className="nav-label">工作空间 <span>WORKSPACE</span></div>
      <nav aria-label="主导航">
        <Menu theme="dark" mode="inline" className="sider-menu" selectedKeys={newRun ? [] : [selectedKey]}
          items={menuItems} onClick={({ key }) => { navigate(key); setNavOpen(false); }} />
      </nav>
      <div className="sider-footer">
        <SafetyCertificateOutlined className="footer-shield" />
        <div className="sider-footer-title">在授权边界内探索</div>
        <p>仅用于已获授权目标的<br />隔离评测环境。</p>
        <div className="sider-version"><span>ATTACKER CONSOLE</span><span>v0.1</span></div>
      </div>
    </div>
  );

  return (
    <Layout className="app-shell">
      <a href="#main-content" className="skip-link">跳到主要内容</a>
      <Sider width={224} theme="dark" className="sider">{navigation}</Sider>
      <Drawer title="工作空间" placement="left" open={navOpen && !screens.lg} onClose={() => setNavOpen(false)}
        className="nav-drawer" styles={{ body: { padding: 0 } }} size={280}>
        {navigation}
      </Drawer>
      <Layout className="main-layout">
        <Header className="app-header">
          <div className="header-location">
            <Button className="mobile-menu-button" type="text" icon={<MenuOutlined />} aria-label="打开导航" onClick={() => setNavOpen(true)} />
            <span className="breadcrumb-root">工作空间</span><span className="breadcrumb-divider">/</span><span>{pageName}</span>
          </div>
          <div className="header-tools">
            <Tooltip title={healthy ? `运行环境：${healthQuery.data?.environment ?? "本地"}` : statusText}>
              <span className="connection-status"><span className={`pulse-dot ${statusClass}`} /><span className="connection-label">{statusText}</span></span>
            </Tooltip>
            {!activeCountQuery.isError && typeof activeCountQuery.data === "number" && activeCountQuery.data > 0 && (
              <Link to="/jobs" className="active-jobs">{activeCountQuery.data} 个进行中</Link>
            )}
            <span className="header-separator" />
            <Tooltip title={apiKey ? "已配置 API Key" : "本地开发可留空"}>
              <Button className="key-button" icon={<KeyOutlined />} onClick={() => { setDraftKey(apiKey ?? ""); setKeyModalOpen(true); }}>
                <span className="key-label">{apiKey ? "API Key 已配置" : "设置 API Key"}</span>
              </Button>
            </Tooltip>
          </div>
        </Header>
        <Content className="app-content" id="main-content" tabIndex={-1}>
          <div className="content-inner">
            <Suspense fallback={<div className="page-loading" role="status"><Spin /><p>正在加载页面…</p></div>}>
              <Outlet />
            </Suspense>
          </div>
          <footer className="content-footer"><span>ATTACKER <span className="footer-slash">/</span> AGENT SECURITY</span><span>受控执行 · 证据可追溯</span></footer>
        </Content>
      </Layout>
      <Modal title="服务级 API Key" open={keyModalOpen} onCancel={() => setKeyModalOpen(false)}
        onOk={() => { setApiKey(draftKey); setKeyModalOpen(false); }} okText="保存" cancelText="取消">
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          Key 仅保存在浏览器 localStorage，通过 <code>X-API-Key</code> 头发送。留空并保存可清除。
        </Typography.Paragraph>
        <Input.Password aria-label="服务级 API Key" value={draftKey} onChange={(event) => setDraftKey(event.target.value)} placeholder="ATTACKER_API_KEY" autoComplete="off" />
      </Modal>
    </Layout>
  );
}
