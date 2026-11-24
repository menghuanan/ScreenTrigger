import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// ScreenTrigger 桌面壳前端构建配置（TASK-004 设计 §3.1）。
// - 固定 dev 端口 5173，与 src-tauri/tauri.conf.json 的 build.devUrl 对齐；
// - clearScreen=false 保留 Rust 编译输出，便于 dev 期间观察 sidecar 日志；
// - dist 目录在 dev/build 时重新生成（当前占位 index.html 会被真实构建覆盖）。
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
  build: {
    target: "chrome105",
    outDir: "dist",
    emptyOutDir: true,
  },
});
