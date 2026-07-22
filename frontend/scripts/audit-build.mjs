import { createHash } from "node:crypto";
import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";

const distRoot = path.resolve("dist");
const forbiddenStarterMarkers = [
  "Vite + React",
  "react.svg",
  "vite.svg",
  "Edit src/App",
  "/src/main.tsx",
];

async function filesBeneath(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const resolved = path.join(directory, entry.name);
      return entry.isDirectory() ? await filesBeneath(resolved) : [resolved];
    }),
  );
  return nested.flat().sort();
}

const files = await filesBeneath(distRoot);
if (files.length === 0) {
  throw new Error("The production build is empty.");
}
if (files.some((file) => file.endsWith(".map"))) {
  throw new Error("Production source maps are not approved.");
}

const indexHtml = await readFile(path.join(distRoot, "index.html"), "utf8");
if (!/<meta\s+name="referrer"\s+content="no-referrer"\s*\/>/u.test(indexHtml)) {
  throw new Error("The production entry point must enforce a no-referrer policy.");
}

const inventory = [];
for (const file of files) {
  const bytes = await readFile(file);
  const text = /\.(?:css|html|js|json)$/u.test(file) ? bytes.toString("utf8") : "";
  for (const marker of forbiddenStarterMarkers) {
    if (text.includes(marker)) {
      throw new Error(`Starter content remains in ${path.relative(distRoot, file)}.`);
    }
  }
  inventory.push({
    path: path.relative(distRoot, file).replaceAll("\\", "/"),
    bytes: (await stat(file)).size,
    sha256: createHash("sha256").update(bytes).digest("hex"),
  });
}

process.stdout.write(`${JSON.stringify({ status: "PASS", files: inventory }, null, 2)}\n`);
