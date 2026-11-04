import { mkdirSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { compile } from "sass";

const result = compile(fileURLToPath(new URL("./tail-accordion.scss", import.meta.url)), {
  loadPaths: [fileURLToPath(new URL("../node_modules/", import.meta.url))],
  style: "compressed",
  quietDeps: true,
  silenceDeprecation: ["import"],
});
const directory = new URL("../static/vendor/", import.meta.url);
mkdirSync(directory, { recursive: true });
const banner = "/*! Bootstrap 5.3.8 Accordion | MIT License | https://github.com/twbs/bootstrap/blob/main/LICENSE | Scoped build; no Bootstrap JavaScript */\n";
writeFileSync(new URL("tail-accordion.css", directory), `${banner}${result.css}\n`);
