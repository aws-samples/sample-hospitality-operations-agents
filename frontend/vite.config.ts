// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', sourcemap: false },
  server: {
    // `npm run dev` against the deployed API. The console always calls /api/*
    // relative, because in production CloudFront serves both the app and the API
    // from one origin -- so local development has to fake that same shape rather
    // than introduce an absolute base URL the production build would not use.
    proxy: process.env.VITE_API_PROXY
      ? { '/api': { target: process.env.VITE_API_PROXY, changeOrigin: true } }
      : undefined,
  },
});
