import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readDevConfig } from './dev-config.mjs';

export default defineConfig(({ command }) => {
  // 正式构建不读取本机开发地址；部署域名由 HTTPS 网关配置。
  const dev = command === 'serve' ? readDevConfig() : undefined;
  return {
    plugins: [react()],
    server: dev ? {
      host: dev.webHost, port: dev.webPort, strictPort: true,
      proxy: { '/api': dev.apiUrl, '/health': dev.apiUrl }
    } : undefined,
    build: { rollupOptions: { output: { manualChunks(id) {
      if(id.includes('node_modules')) return ['/node_modules/react/','/node_modules/react-dom/','/node_modules/scheduler/','/node_modules/react-is/'].some(p=>id.includes(p))?'react':'ui';
    } } }, chunkSizeWarningLimit: 1200 }
  };
});
