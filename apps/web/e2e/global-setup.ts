import { createDistServer } from "./server";
import { startStack, stopStack } from "./real/stack";

export default async function globalSetup() {
  // Local real stack: the production API with a scripted provider, plus the built web bundle.
  if (process.env.E2E_REAL_STACK === "scripted") {
    await startStack();
    return () => stopStack();
  }

  // Target an already-running deployment (for example the nginx image) instead.
  if (process.env.E2E_BASE_URL) return;

  const server = createDistServer();
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(4173, "127.0.0.1", () => resolve());
  });

  return async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
    });
  };
}
