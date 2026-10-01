import { Suspense, useState, type CSSProperties } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  ArrowUpRight,
  Boxes,
  Eye,
  EyeOff,
  KeyRound,
  LayoutDashboard,
  ListTodo,
  Plus,
  ShieldCheck,
  X,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getHealth, listJobs } from "@/api/client";
import { useAuthStore } from "@/lib/auth";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const menuItems = [
  { to: "/dashboard", icon: LayoutDashboard, label: "评测总览" },
  { to: "/jobs", icon: ListTodo, label: "任务队列" },
  { to: "/approvals", icon: ShieldCheck, label: "审批中心" },
  { to: "/equipment", icon: Boxes, label: "装备目录" },
];

function WorkspaceSidebar({
  selectedKey,
  activeCount,
}: {
  selectedKey: string;
  activeCount?: number;
}) {
  const { setOpenMobile } = useSidebar();
  const closeNavigation = () => setOpenMobile(false);
  return (
    <Sidebar>
      <SidebarHeader className="px-5 pt-6 pb-5">
        <div className="flex items-center justify-between">
          <Link
            className="brand"
            to="/dashboard"
            onClick={closeNavigation}
            aria-label="Attacker 总览"
          >
            <div className="brand-logo">
              <img src="/logo.png" alt="" />
            </div>
            <div>
              <div className="brand-title">
                Attacker<span className="brand-period">.</span>
              </div>
              <div className="brand-subtitle">Agent Security Console</div>
            </div>
          </Link>
          <Button
            variant="ghost"
            size="icon"
            className="md:hidden"
            aria-label="关闭导航"
            onClick={closeNavigation}
          >
            <X data-icon="inline-start" />
          </Button>
        </div>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup className="px-4">
          <SidebarGroupLabel>工作空间</SidebarGroupLabel>
          <SidebarGroupContent>
            <nav aria-label="主导航">
              <SidebarMenu className="gap-1">
                {menuItems.map(({ to, icon: Icon, label }) => (
                  <SidebarMenuItem key={to}>
                    <SidebarMenuButton
                      asChild
                      isActive={selectedKey === to}
                      className="h-10 px-3 gap-3"
                    >
                      <Link
                        to={to}
                        onClick={closeNavigation}
                        aria-current={selectedKey === to ? "page" : undefined}
                      >
                        <Icon />
                        <span>{label}</span>
                      </Link>
                    </SidebarMenuButton>
                    {to === "/jobs" && !!activeCount && (
                      <SidebarMenuBadge>{activeCount}</SidebarMenuBadge>
                    )}
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </nav>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarGroup className="px-4 pt-5">
          <SidebarGroupLabel>评测</SidebarGroupLabel>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                asChild
                isActive={selectedKey === "/runs/new"}
                className="h-10 px-3 gap-3"
              >
                <Link
                  to="/runs/new"
                  onClick={closeNavigation}
                  aria-current={
                    selectedKey === "/runs/new" ? "page" : undefined
                  }
                >
                  <Plus />
                  <span>新建评测</span>
                </Link>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="px-6 pb-6 gap-4">
        <div className="sidebar-note">
          <ShieldCheck aria-hidden="true" />
          <strong>在授权边界内探索</strong>
          <p>
            仅用于已获授权目标的
            <br />
            隔离评测环境。
          </p>
        </div>
        <Separator />
        <div className="sidebar-version">
          <span>Attacker Console</span>
          <Badge variant="outline">v0.1</Badge>
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}

export default function AppLayout() {
  const location = useLocation();
  const { apiKey, setApiKey } = useAuthStore();
  const [keyModalOpen, setKeyModalOpen] = useState(false);
  const [draftKey, setDraftKey] = useState("");
  const [showKey, setShowKey] = useState(false);
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
      jobs.filter((j) =>
        ["queued", "leased", "running", "retry_wait"].includes(j.status),
      ).length,
  });
  const healthy =
    healthQuery.data?.status === "ready" || healthQuery.data?.status === "ok";
  const newRun = location.pathname === "/runs/new";
  const selectedKey = newRun
    ? "/runs/new"
    : (menuItems.find((item) => location.pathname.startsWith(item.to))?.to ??
      "/jobs");
  const pageName = newRun
    ? "新建评测"
    : location.pathname.startsWith("/runs/")
      ? "评测详情"
      : menuItems.find((item) => item.to === selectedKey)?.label;
  const statusText = healthQuery.isError
    ? "控制面离线"
    : healthQuery.isLoading
      ? "正在连接"
      : healthy
        ? "控制面在线"
        : "依赖未就绪";

  return (
    <TooltipProvider>
      <SidebarProvider style={{ "--sidebar-width": "15rem" } as CSSProperties}>
        <a href="#main-content" className="skip-link">
          跳到主要内容
        </a>
        <WorkspaceSidebar
          selectedKey={selectedKey}
          activeCount={
            activeCountQuery.isError ? undefined : activeCountQuery.data
          }
        />
        <SidebarInset className="min-w-0">
          <header className="app-header">
            <div className="header-location">
              <SidebarTrigger aria-label="切换导航" />
              <Separator orientation="vertical" className="h-4" />
              <Breadcrumb aria-label="页面位置">
                <BreadcrumbList>
                  <BreadcrumbItem className="hidden md:inline-flex">
                    <BreadcrumbLink asChild>
                      <Link to="/dashboard">工作空间</Link>
                    </BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator className="hidden md:block" />
                  <BreadcrumbItem>
                    <BreadcrumbPage>{pageName}</BreadcrumbPage>
                  </BreadcrumbItem>
                </BreadcrumbList>
              </Breadcrumb>
            </div>
            <div className="header-tools">
              <Tooltip>
                <TooltipTrigger asChild>
                  <span
                    className="connection-status"
                    tabIndex={0}
                    aria-label={statusText}
                  >
                    <span
                      className={cn(
                        "status-dot",
                        healthQuery.isError
                          ? "is-offline"
                          : healthy
                            ? "is-online"
                            : "is-pending",
                      )}
                    />
                    <span className="connection-label">{statusText}</span>
                  </span>
                </TooltipTrigger>
                <TooltipContent>
                  {healthy
                    ? `运行环境：${healthQuery.data?.environment ?? "本地"}`
                    : statusText}
                </TooltipContent>
              </Tooltip>
              <Dialog
                open={keyModalOpen}
                onOpenChange={(open) => {
                  setKeyModalOpen(open);
                  if (open) {
                    setDraftKey(apiKey ?? "");
                    setShowKey(false);
                  }
                }}
              >
                <DialogTrigger asChild>
                  <Button
                    variant="outline"
                    size="sm"
                    aria-label={apiKey ? "API Key 已配置" : "设置 API Key"}
                  >
                    <KeyRound data-icon="inline-start" />
                    <span className="key-label">
                      {apiKey ? "API Key 已配置" : "设置 API Key"}
                    </span>
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>服务级 API Key</DialogTitle>
                    <DialogDescription>
                      配置控制台访问后端服务时使用的凭据。
                    </DialogDescription>
                  </DialogHeader>
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      setApiKey(draftKey);
                      setKeyModalOpen(false);
                    }}
                    className="flex flex-col gap-6"
                  >
                    <FieldGroup>
                      <Field>
                        <FieldLabel htmlFor="service-api-key">
                          API Key
                        </FieldLabel>
                        <InputGroup>
                          <InputGroupInput
                            id="service-api-key"
                            type={showKey ? "text" : "password"}
                            value={draftKey}
                            onChange={(event) =>
                              setDraftKey(event.target.value)
                            }
                            placeholder="输入 API Key"
                            autoComplete="off"
                            aria-describedby="key-description"
                          />
                          <InputGroupAddon align="inline-end">
                            <InputGroupButton
                              size="icon-xs"
                              aria-label={
                                showKey ? "隐藏 API Key" : "显示 API Key"
                              }
                              aria-pressed={showKey}
                              onClick={() => setShowKey(!showKey)}
                            >
                              {showKey ? (
                                <EyeOff data-icon="inline-start" />
                              ) : (
                                <Eye data-icon="inline-start" />
                              )}
                            </InputGroupButton>
                          </InputGroupAddon>
                        </InputGroup>
                        <FieldDescription id="key-description">
                          仅保存在当前浏览器，通过 X-API-Key
                          请求头发送。留空并保存可清除。
                        </FieldDescription>
                      </Field>
                    </FieldGroup>
                    <DialogFooter>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => setKeyModalOpen(false)}
                      >
                        取消
                      </Button>
                      <Button type="submit">保存配置</Button>
                    </DialogFooter>
                  </form>
                </DialogContent>
              </Dialog>
            </div>
          </header>
          <div className="app-content" id="main-content" tabIndex={-1}>
            <div className="content-inner">
              <Suspense
                fallback={
                  <div
                    className="flex flex-col gap-6"
                    role="status"
                    aria-label="正在加载页面"
                  >
                    <Skeleton className="h-9 w-44" />
                    <Skeleton className="h-4 w-72 max-w-full" />
                    <Skeleton className="h-64 w-full" />
                  </div>
                }
              >
                <Outlet />
              </Suspense>
            </div>
            <footer className="content-footer">
              <span>
                <Activity aria-hidden="true" />
                受控执行 · 证据可追溯
              </span>
              <Link to="/equipment">
                评测装备
                <ArrowUpRight aria-hidden="true" />
              </Link>
            </footer>
          </div>
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
  );
}
