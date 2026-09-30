import { defineConfig } from '@hey-api/openapi-ts'

// 输入由 `bun run generate-client` 从 FastAPI 导出; 输出为生成代码, 不手改
export default defineConfig({
  input: './openapi.json',
  output: 'src/client',
  plugins: ['@hey-api/client-fetch', '@tanstack/react-query'],
})
