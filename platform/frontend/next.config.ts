import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // 个人档要把 UI 装进一个 Python wheel 里：构建期用 Node，运行期不许再需要它。
  // `output: "export"` 产出纯静态文件，由 App Server 自己 serve。
  // 环境变量而不是写死：`next dev` 与本地跨端口调试仍要 rewrites（export 模式
  // 不支持 rewrites），那条路是开发用的，不该被交付形态绑架。
  ...(process.env.PLATFORM_STATIC_EXPORT ? { output: "export" as const } : {}),
  // 本地跨端口代理必须保留 SSE 的逐事件传输；Next 压缩会缓冲小事件。
  // 生产环境没有 PLATFORM_API_PROXY_TARGET，仍保留默认压缩。
  compress: !process.env.PLATFORM_API_PROXY_TARGET,
  // Next 默认会把带尾斜杠的路径 308 到不带斜杠的那一份 —— 对页面路由是对的，
  // 对被 rewrite 出去的 /api/* 是错的：后端的集合端点就叫 `/projects/`，斜杠
  // 被剥掉后 FastAPI 又 307 加回来，而它的 Location 是**内网绝对地址**
  // （http://127.0.0.1:8000/...）。浏览器跟着跳就成了跨源跳转，
  // 按规范会丢掉 Authorization 头 —— 于是每个集合端点都 401，前端把 401
  // 当成掉线直接弹回登录页。同源 nginx 拓扑下这一步是本地跳转所以从不发作，
  // 本地跨端口调试（这份配置存在的唯一理由）却整个 Projects 页都打不开。
  skipTrailingSlashRedirect: true,
  // 本地跨端口调试时由 Next 转发 API，避免浏览器 CORS；生产环境不设置
  // PLATFORM_API_PROXY_TARGET，仍使用 nginx 的同源 /api/v1。
  async rewrites() {
    const target = process.env.PLATFORM_API_PROXY_TARGET;
    if (!target) return [];
    const base = target.replace(/\/$/, "");
    // 两条规则缺一不可：`:path*` 会把尾斜杠当成空段吃掉，于是后端集合端点
    // `/projects/` 被转成 `/projects`，FastAPI 再 307 加回来。带斜杠的那条要排在前面。
    return [
      { source: "/api/:path*/", destination: `${base}/api/:path*/` },
      { source: "/api/:path*", destination: `${base}/api/:path*` },
    ];
  },
};

export default nextConfig;
