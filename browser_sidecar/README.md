# KT6 Playwright/CDP 只读采集 Sidecar

该工具连接测试区内已经启动的 Chromium 调试端口，读取
`DOMSnapshot.captureSnapshot` 和 `Accessibility.getFullAXTree`，生成供 KT6
归一化的原始快照。它不会调用点击、键盘输入、网络拦截或页面脚本执行接口。

## 安装与运行

```powershell
cd browser_sidecar
npm install
node .\capture-ui-graph.mjs `
  --cdp-url http://127.0.0.1:9222 `
  --page-url nce `
  --output .\cdp-snapshot.json
```

Chromium 需要由测试人员显式启用本机调试端口。Sidecar 只接受 loopback CDP
地址，输出文件使用独占创建模式，避免静默覆盖已有采集结果。

为避免采错页面，`--page-url` 必须只匹配一个页面；不传时也必须只有一个
非 `chrome://` 页面。帧数超过 64、DOM 节点总数超过 10000 或可访问性节点
累计超过 10000 时，采集会直接失败而不是静默截断。页面标题从只读的
`DOMSnapshot` 主文档中解码，不额外调用页面标题 API。

采集结果中的 `isClickable`、ARIA role 和 focusable 只用于候选生成，始终带
`safe_for_execution=false`。后续真实动作仍需通过 KT6 的资产绑定、新鲜页面复核、
权限确认和一次性 token 流程。
