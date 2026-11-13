import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND =
  process.env.VITE_BACKEND_ORIGIN || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],

  server: {
    host: "0.0.0.0",
    port: 5173,
    allowedHosts: true,
    // Behind an HTTPS tunnel or preview proxy the browser has to reach the
    // HMR socket on 443 over wss. Locally that same setting points the socket
    // at https://127.0.0.1, so hot reload never connects and the console fills
    // with retries. Opt in with VITE_HMR_CLIENT_PORT=443 when tunnelling.
    hmr: process.env.VITE_HMR_CLIENT_PORT
      ? { clientPort: Number(process.env.VITE_HMR_CLIENT_PORT), protocol: "wss" }
      : true,

    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});