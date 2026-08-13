# 本地部署指南

## 快速开始

这是一个纯静态的前端应用，可以通过多种方式部署。

### 方式一：使用 Python 简易服务器

```bash
# 在项目根目录下运行
python3 -m http.server 8000
```

然后访问 `http://localhost:8000`

### 方式二：使用 Node.js 静态服务器

```bash
# 安装 http-server（如果还没有）
npm install -g http-server

# 在项目根目录下运行
http-server -p 8000
```

然后访问 `http://localhost:8000`

### 方式三：使用 Nginx

将项目文件复制到 Nginx 的 web 根目录，例如：

```bash
cp -r . /usr/share/nginx/html/chatnest
```

配置 Nginx：

```nginx
server {
    listen 80;
    server_name localhost;
    
    location / {
        root /usr/share/nginx/html/chatnest;
        index index.html;
        try_files $uri $uri/ /index.html;
    }
}
```

### 方式四：GitHub Pages

1. 在 GitHub 仓库设置中启用 GitHub Pages
2. 选择 `main` 分支作为源
3. 访问 `https://your-username.github.io/chatnest-ui`

## 功能说明

### 演示模式

当前代码包含演示模式（`window.AGENT_APP_DEMO`），所有 API 请求都被拦截并返回模拟数据。

### 连接真实后端

要连接真实后端，需要：

1. 删除或注释掉 `index.html` 中 `window.AGENT_APP_DEMO` 相关的所有代码（约 150 行）
2. 确保后端 API 服务运行在正确的地址
3. 如果后端不在同一域名，需要配置 CORS

### 主要功能

- ✅ 对话管理（新建、重命名、删除、星标）
- ✅ 流式输出
- ✅ 思考链显示
- ✅ 工具调用（搜索、读写文件等）
- ✅ 消息编辑和重新生成
- ✅ 附件上传
- ✅ 历史记录搜索
- ✅ 个人资料和记忆管理
- ✅ 日记和日历功能
- ✅ 响应式设计（支持移动端）

## 环境变量

如果连接真实后端，可能需要配置以下环境变量：

- `API_BASE_URL`: 后端 API 地址（默认为相对路径 `/api`）
- `ENABLE_DEMO`: 是否启用演示模式

## 浏览器兼容性

- Chrome/Edge 90+
- Safari 14+
- Firefox 88+
- 移动端 Safari (iOS 14+)
- 移动端 Chrome (Android 9+)

## 故障排除

### 页面空白
- 检查浏览器控制台是否有错误
- 确保 `index.html` 文件完整且未损坏
- 尝试清除浏览器缓存

### API 请求失败
- 检查后端服务是否运行
- 检查网络请求是否被 CORS 阻止
- 确认 API 路径配置正确

### 样式显示异常
- 确保所有 CSS 都已加载
- 检查是否有 CSP（内容安全策略）限制
- 尝试硬刷新（Ctrl+Shift+R 或 Cmd+Shift+R）

## 开发说明

### 代码结构

```
.
├── index.html          # 主文件（包含所有 HTML/CSS/JS）
├── README.md           # 项目说明
└── DEPLOY.md          # 部署指南（本文件）
```

### 技术栈

- 纯 HTML/CSS/JavaScript（无框架依赖）
- IndexedDB（本地消息缓存）
- Server-Sent Events（流式响应）
- Fetch API（网络请求）

### 自定义修改

主要配置项在 `index.html` 的 `<script>` 部分：

```javascript
// 消息页面大小
const HISTORY_PAGE = 40;

// 长按触发时间
const LONG_PRESS_MS = 360;

// 抽屉手势阈值
const MOVE_CANCEL_PX = 10;
```

## 安全说明

- 所有敏感数据（token、密码）存储在 localStorage
- 建议在生产环境使用 HTTPS
- 定期更新依赖和检查安全漏洞
- 对用户输入进行适当验证

## 许可证

请参考仓库中的 LICENSE 文件。
