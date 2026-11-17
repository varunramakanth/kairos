import { defineConfig } from "@hey-api/openapi-ts";

// Generates src/client from the FastAPI schema. Run `just gen-client` from the repo
// root; never edit src/client by hand.
export default defineConfig({
  input: "./openapi.json",
  output: { path: "src/client" },
  plugins: ["@hey-api/client-fetch", "@hey-api/typescript", "@hey-api/sdk"],
});
