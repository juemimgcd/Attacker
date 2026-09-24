# Attacker Console (React)

面向 AI Agent 授权安全评测平台 **Attacker** 的专业前端控制台。

## 技术栈

- **React 19 + TypeScript + Vite**
- **shadcn/ui（Radix Nova）+ Tailwind CSS 4**（Neutral 浅色主题、Geist 本地字体）
- **Ant Design 6**（保留表单状态/校验引擎与数据表格，界面控件使用 shadcn）
- **TanStack Query**（服务端状态：轮询、缓存、失效）
- **Zustand**（API Key 等本地状态，persist 到 localStorage）
- **React Router**（路由级代码分割）
- **axios**（统一 `X-API-Key` 注入与 FastAPI `detail` 错误归一化）
- **Sonner**（操作结果通知）

## 功能

| 页面 | 能力 | 对接 API |
|---|---|---|
| 总览 | 进行中/失败/完成率统计、最近任务 | `GET /jobs` |
| 任务队列 | 状态筛选、取消、重试、结果查看 | `GET/POST /jobs/*` |
| 新建评测 | 四类 Run 表单（黑盒/灰盒/自适应/带状态），同步执行或提交持久队列 | `POST /runs/*`、`POST /jobs` |
| Run 详情 | 进度、Finding、Case 结果、证据时间线、审批处理、Replay 差异、Markdown 报告 | `GET /runs/{id}` 等 |
| 审批中心 | 聚合进行中 Run 的 pending 审批 | `GET /runs/{id}/approvals` |
| 装备目录 | Provider/Skill/Case Pack/Benchmark 浏览 | `GET /equipment/*` |

## 开发

```bash
npm install
npm run dev        # http://localhost:5173，API 代理到 127.0.0.1:8000
```

后端需先启动（`uvicorn main:app --port 8000`）。若后端配置了 `SECURITY__API_KEY`，
在页面右上角「设置 API Key」填入即可，Key 只存浏览器 localStorage。

## 构建

```bash
npm run lint       # oxlint
npm run build      # TypeScript 检查并生成 dist/
npm run preview    # 预览构建产物
```

## 设计约束

- 不引入登录体系：后端是单服务级 API Key，无用户/RBAC，前端不做越权包装。
- Finding 必须关联 Evidence 才展示关联率，遵循后端 "Evidence before claims" 原则。
- 公网 Target 默认禁止，表单显式提供 `allow_public_target` 开关并提示授权前提。

## UI 约定

- 官方组件配置见 `components.json`；添加组件使用 `npx shadcn@latest add <component>`。
- 语义色、字体、间距和页面布局集中在 `src/styles/global.css`；`main.tsx` 从同一套 tokens 生成 Ant Design 主题。
- CSS 按 `theme → base → antd → components → utilities` 排序，避免 Ant Design 覆盖 shadcn 交互状态。
- 桌面侧栏支持折叠，手机使用抽屉导航；数据表格在窄屏内独立横向滚动。
- 新建评测使用 `Field` 统一标签、说明和错误提示；列表次要操作使用 `DropdownMenu`，取消任务使用 `AlertDialog`。
- 结构化详情使用 `Dialog` + `ScrollArea`，提供复制 JSON；评测详情用 `Tabs`、`Progress` 与统一指标栏组织结果。
- 当前后端 `/console` 是独立的原生控制台；本 React 界面开发时访问 Vite 地址。生产环境需要单独配置 `dist/` 静态挂载和 API 代理。
