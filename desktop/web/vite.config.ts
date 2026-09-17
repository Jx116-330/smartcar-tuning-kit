import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 产物给纯静态托管用(桥 serve /ui),base 必须相对路径
export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
  },
  server: {
    // 开发期:vite dev 把 API 代理到桥(桥需先启动)
    proxy: {
      '/events': { target: 'http://127.0.0.1:9898', changeOrigin: true },
      '/schema': { target: 'http://127.0.0.1:9898', changeOrigin: true },
      '/config': { target: 'http://127.0.0.1:9898', changeOrigin: true },
      '/snapshot': { target: 'http://127.0.0.1:9898', changeOrigin: true },
      '/latest': { target: 'http://127.0.0.1:9898', changeOrigin: true },
      '/batch': { target: 'http://127.0.0.1:9898', changeOrigin: true },
      '/command': { target: 'http://127.0.0.1:9898', changeOrigin: true },
      '/trajectory': { target: 'http://127.0.0.1:9898', changeOrigin: true },
      '/paths': { target: 'http://127.0.0.1:9898', changeOrigin: true },
    },
  },
});
