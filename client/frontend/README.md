# Dashboard 前端

本目录保存 React、TypeScript 和 Vite 源码。Python Dashboard Server 直接提供仓库中的
`client/dist/`，因此修改源码后必须同步提交构建产物。

```powershell
npm install
npm run build
cd ..\..
node .\control\deployment_checks\check-dashboard.mjs
```

本地联调可在 Dashboard Server 已运行时执行 `npm run dev`。Vite 将 `/api` 代理到默认
Dashboard 地址 `http://127.0.0.1:4178`；该代理仅用于开发，生产服务不依赖 Vite。
