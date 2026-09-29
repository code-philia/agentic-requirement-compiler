"""Format generated TypeScript with the initialized project's own compiler."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .process_utils import resolve_executable


FORMAT_SCRIPT = r"""
const ts = require(require.resolve("typescript", { paths: [process.cwd()] }));
const fs = require("node:fs");
const nodePath = require("node:path");
const sources = JSON.parse(fs.readFileSync(0, "utf8"));
const printer = ts.createPrinter({ newLine: ts.NewLineKind.LineFeed, preserveSourceNewlines: true });
const output = {};
for (const [path, text] of Object.entries(sources)) {
  const file = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true,
    path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  if (file.parseDiagnostics.length) {
    throw new Error(path + ": " + file.parseDiagnostics.map(d =>
      ts.flattenDiagnosticMessageText(d.messageText, "\n")).join("\n"));
  }
  let printed = printer.printFile(file);
  const fileName = nodePath.resolve(path);
  const service = ts.createLanguageService({
    getCompilationSettings: () => ({}),
    getScriptFileNames: () => [fileName],
    getScriptVersion: () => "0",
    getScriptSnapshot: name => name === fileName ? ts.ScriptSnapshot.fromString(printed) : undefined,
    getCurrentDirectory: () => process.cwd(),
    getDefaultLibFileName: options => ts.getDefaultLibFilePath(options),
    fileExists: ts.sys.fileExists,
    readFile: ts.sys.readFile,
  });
  const edits = service.getFormattingEditsForDocument(fileName, {
    ...ts.getDefaultFormatCodeSettings(), indentSize: 4, tabSize: 4,
    convertTabsToSpaces: true, indentStyle: ts.IndentStyle.Smart, newLineCharacter: "\n",
  });
  service.dispose();
  for (const edit of edits.sort((a, b) => b.span.start - a.span.start)) {
    printed = printed.slice(0, edit.span.start) + edit.newText + printed.slice(edit.span.start + edit.span.length);
  }
  output[path] = printed;
}
process.stdout.write(JSON.stringify(output));
"""


def format_typescript(sources: dict[str, str], project_root: Path) -> dict[str, str]:
    node = resolve_executable("node", os.environ)
    if node is None:
        raise ValueError("TypeScript source formatting failed: node is unavailable")
    result = subprocess.run(
        [node, "-e", FORMAT_SCRIPT], cwd=project_root, input=json.dumps(sources, ensure_ascii=False),
        text=True, encoding="utf-8", capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError("TypeScript source formatting failed: " + result.stderr.strip())
    return json.loads(result.stdout)
