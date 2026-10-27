import { defineConfig } from "astro/config";

export default defineConfig({
  site: "https://example.com",
  trailingSlash: "always",
  build: { format: "directory", inlineStylesheets: "always" },
});
