import { createReadStream, statSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";

const contentTypes: Record<string, string> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

export function createDistServer(): Server {
  const root = resolve("dist");
  return createServer((request, response) => {
    const requestPath = decodeURIComponent(
      new URL(request.url ?? "/", "http://127.0.0.1").pathname,
    );
    const relativePath = normalize(requestPath).replace(/^[/\\]+/, "");
    let filePath = join(root, relativePath || "index.html");

    try {
      if (!filePath.startsWith(`${root}${sep}`) || !statSync(filePath).isFile()) {
        filePath = join(root, "index.html");
      }
    } catch {
      filePath = join(root, "index.html");
    }

    response.writeHead(200, {
      "Cache-Control": "no-store",
      "Content-Type": contentTypes[extname(filePath)] ?? "application/octet-stream",
    });
    createReadStream(filePath).pipe(response);
  });
}
