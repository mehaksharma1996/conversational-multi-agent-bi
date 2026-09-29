import { createDistServer } from "./server";

export default async function globalSetup() {
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
