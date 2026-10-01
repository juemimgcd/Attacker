import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Skeleton } from "@/components/ui/skeleton";
import AppLayout from "@/layouts/AppLayout";

// 路由级代码分割，首屏只加载布局与当前页。
const DashboardPage = lazy(() => import("@/pages/DashboardPage"));
const JobsPage = lazy(() => import("@/pages/JobsPage"));
const NewRunPage = lazy(() => import("@/pages/NewRunPage"));
const RunDetailPage = lazy(() => import("@/pages/RunDetailPage"));
const ApprovalsPage = lazy(() => import("@/pages/ApprovalsPage"));
const EquipmentPage = lazy(() => import("@/pages/EquipmentPage"));

const fallback = (
  <div
    className="flex flex-col gap-6 p-10"
    role="status"
    aria-label="正在加载控制台"
  >
    <Skeleton className="h-10 w-48" />
    <Skeleton className="h-72 w-full" />
  </div>
);

export default function App() {
  return (
    <Suspense fallback={fallback}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/runs/new" element={<NewRunPage />} />
          <Route path="/runs/:runId" element={<RunDetailPage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route path="/equipment" element={<EquipmentPage />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
