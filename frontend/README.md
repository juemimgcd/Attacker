# Attacker Console (React)

面向 AI Agent 授权安全评测平台 **Attacker** 的专业前端控制台。

## 技术栈

- **React 19 + TypeScript + Vite**
- **Ant Design 6**（深色主题定制）
- **TanStack Query**（服务端状态：轮询、缓存、失效）
- **Zustand**（API Key 等本地状态，persist 到 localStorage）
- **React Router**（路由级代码分割）
- **axios**（统一 `X-API-Key` 注入与 FastAPI `detail` 错误归一化）

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
npm run build      # 产物在 dist/，可由 FastAPI 静态挂载同源部署
```

## 设计约束

- 不引入登录体系：后端是单服务级 API Key，无用户/RBAC，前端不做越权包装。
- Finding 必须关联 Evidence 才展示关联率，遵循后端 "Evidence before claims" 原则。
- 公网 Target 默认禁止，表单显式提供 `allow_public_target` 开关并提示授权前提。
