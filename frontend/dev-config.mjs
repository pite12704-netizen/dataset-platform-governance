// 【学习标注：开发地址】PowerShell 与 Vite 共用这份解析器，地址只在 config/development.json 配置。
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const defaultConfigPath = fileURLToPath(new URL('../config/development.json', import.meta.url));

export function readDevConfig(configPath = process.env.DP_DEV_CONFIG || defaultConfigPath) {
  const config = JSON.parse(readFileSync(configPath, 'utf8').replace(/^\uFEFF/, ''));
  for (const field of ['apiHost', 'webHost']) {
    // 限定主机名/IP，禁止把完整 URL 或命令行参数误填进启动参数。
    if (typeof config[field] !== 'string' || !/^[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*$/.test(config[field])) {
      throw new Error(`${field} must be a hostname or IPv4 address, without a protocol, port or path.`);
    }
  }
  for (const field of ['apiPort', 'webPort']) {
    if (!Number.isInteger(config[field]) || config[field] < 1 || config[field] > 65535) {
      throw new Error(`${field} must be an integer between 1 and 65535.`);
    }
  }
  if (config.apiPort === config.webPort) {
    throw new Error('apiPort and webPort must be different.');
  }
  // 监听所有网卡时，用回环地址做健康检查和本机浏览器访问。
  const apiHost = config.apiHost === '0.0.0.0' ? '127.0.0.1' : config.apiHost;
  const webHost = config.webHost === '0.0.0.0' ? '127.0.0.1' : config.webHost;
  const apiUrl = `http://${apiHost}:${config.apiPort}`;
  const webUrl = `http://${webHost}:${config.webPort}`;
  const corsOrigins = [...new Set([webUrl, ...(['127.0.0.1', 'localhost'].includes(webHost)
    ? [`http://localhost:${config.webPort}`, `http://127.0.0.1:${config.webPort}`] : [])])];
  return { ...config, apiUrl, webUrl, corsOrigins };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    process.stdout.write(JSON.stringify(readDevConfig(process.argv[2])));
  } catch (error) {
    process.stderr.write(`Invalid development configuration: ${error.message}\n`);
    process.exitCode = 1;
  }
}
