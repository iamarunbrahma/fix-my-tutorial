// Copy the self-hosted fonts and the Basecoat component scripts the pages use into the app's static folder,
// together with their licenses.
import { copyFile, mkdir } from "node:fs/promises";

const STATIC = new URL("../src/fixmytutorial/web/static/", import.meta.url);
const NM = new URL("./node_modules/", import.meta.url);

const files = [
  ["geist/dist/fonts/geist-sans/Geist-Variable.woff2", "fonts/Geist-Variable.woff2"],
  ["geist/dist/fonts/geist-mono/GeistMono-Variable.woff2", "fonts/GeistMono-Variable.woff2"],
  ["geist/LICENSE.txt", "fonts/LICENSE-Geist.txt"],
  ["basecoat-css/dist/js/basecoat.min.js", "vendor/basecoat.min.js"],
  ["basecoat-css/dist/js/tabs.min.js", "vendor/tabs.min.js"],
  ["basecoat-css/LICENSE.md", "vendor/LICENSE-Basecoat.md"],
];

for (const [from, to] of files) {
  const dest = new URL(to, STATIC);
  await mkdir(new URL(".", dest), { recursive: true });
  await copyFile(new URL(from, NM), dest);
  console.log("copied", to);
}
