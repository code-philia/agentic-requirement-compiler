"""Lower the seven-entity frontend IR without projecting it into Thin Frontend IR.

The compiler emits React structure, types and explicit unimplemented behavior stubs.
No model calls or interpretation of prose occurs during lowering.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

from .design_projection import project_api_contracts
from .frontend_workspace import references
from .typescript_format import format_typescript


def js(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def ts_type(value: dict[str, Any]) -> str:
    kind = value["kind"]
    if kind in {"string", "number", "boolean", "null"}:
        return kind
    if kind == "integer":
        return "number"
    if kind == "ui":
        return "React.ReactNode"
    if kind == "array":
        return f"Array<{ts_type(value['items'])}>"
    if kind == "union":
        return "(" + " | ".join(ts_type(v) for v in value["variants"]) + ")"
    if kind == "object":
        return "{\n" + ";\n".join(js(f["name"]) + ("" if f["required"] else "?") + ": " + ts_type(f["type"]) for f in value["fields"]) + " }"
    raise ValueError(f"Unsupported type: {kind}")


@dataclass
class ReactLoweringResult:
    sources: dict[str, str] = field(default_factory=dict)
    bindings: list[dict[str, Any]] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    implementation_tasks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def report(self) -> dict[str, Any]:
        return {"status": "LOWERED" if self.ok else "FAILED", "errors": self.errors, "warnings": self.warnings,
                "files": sorted(self.sources), "bindings": self.bindings,
                "implementation_tasks": self.implementation_tasks, "implementation_status": "SKELETON"}


class FrontendReactLowerer:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def lower(self, ir: dict[str, Any], backend_design: dict[str, Any],
              backend_routes: dict[str, Any], backend_port: int) -> ReactLoweringResult:
        self.result = ReactLoweringResult()
        self.ir = ir
        self.rows: dict[str, dict[str, Any]] = {}
        self.apis = {r["id"]: r for r in project_api_contracts(backend_design)}
        self.routes = {r["module_id"]: r for r in backend_routes.get("routes", [])}
        try:
            for table in ("components", "data", "properties", "ui", "events", "handlers", "effects"):
                for row in ir[table]:
                    if row["id"] in self.rows:
                        raise ValueError(f"Duplicate entity: {row['id']}")
                    self.rows[row["id"]] = row
            if ir["root_component_id"] not in {r["id"] for r in ir["components"]}:
                raise ValueError("Root component is missing")
            for row in self.rows.values():
                missing = [key for _, key in references(row) if key not in self.rows]
                if missing:
                    raise ValueError(f"{row['id']}: missing references {missing}")
            def visit(key: str, ancestors: set[str]) -> None:
                if key in ancestors:
                    raise ValueError(f"Recursive component composition: {key}")
                for ui in ir["ui"]:
                    if ui["component_id"] == key and ui["kind"] == "COMPONENT":
                        visit(ui["component_ref"], ancestors | {key})
            for component in ir["components"]:
                visit(component["id"], set())
            self.component_names = self._component_names(ir["components"])
            self._plan_symbols()
            self._api_source()
            for component in ir["components"]:
                try:
                    self._component(component)
                except (ValueError, KeyError, TypeError, RecursionError) as exc:
                    self.result.errors.append(f"{component['id']}: {exc}")
            root = self.component_names[ir["root_component_id"]]
            required = [d for d in self.data(ir["root_component_id"], "INPUT") if d["required"] and d.get("default") is None]
            if required:
                raise ValueError("Root has unsupplied required inputs: " + ", ".join(d["id"] for d in required))
            self.result.sources["frontend/src/App.tsx"] = f'import {{ {root} as RootComponent }} from "./components/{root}";\nexport default function App() {{ return <RootComponent />; }}\n'
            self.result.sources["frontend/src/runtime/effects.ts"] = EFFECT_RUNTIME
            self.result.sources["frontend/vite.config.ts"] = (
                'import { defineConfig } from "vite";\nimport react from "@vitejs/plugin-react";\n'
                'import tailwindcss from "@tailwindcss/vite";\n'
                f'const proxy = {{ "/api": {{ target: "http://127.0.0.1:{int(backend_port)}", changeOrigin: true }} }};\n'
                'export default defineConfig({ plugins: [react(), tailwindcss()], server: { proxy }, preview: { proxy } });\n'
            )
        except (ValueError, KeyError, TypeError) as exc:
            self.result.errors.append(str(exc))
        if self.result.ok:
            try:
                self.result.sources = format_typescript(self.result.sources, self.project_root)
            except (OSError, ValueError) as exc:
                self.result.errors.append(str(exc))
        return self.result

    def data(self, owner: str, direction: str) -> list[dict[str, Any]]:
        return [r for r in self.ir["data"] if r["owner_id"] == owner and r["direction"] == direction]

    def signature(self, owner: str, direction: str) -> str:
        return "{\n" + ";\n".join(self.member(d["id"]) + ("" if d["required"] and d.get("default") is None else "?") + ": " + ts_type(d["type"]) for d in self.data(owner, direction)) + "\n}"

    def expr(self, value: dict[str, Any] | None) -> str:
        if value is None:
            return "undefined"
        kind = value["kind"]
        if kind == "LITERAL":
            return js(value["value"])
        if kind in {"REF", "ITEM"}:
            key = value["ref_id"] if kind == "REF" else value["ui_id"]
            row = self.rows[key]
            base = self.symbol(key) if kind == "REF" else "item_" + self.symbol(key)
            if kind == "REF" and row.get("kind") == "REF":
                base += ".current"
            return base + "".join("[" + js(p) + "]" for p in value.get("path", []))
        if kind == "UI_REF":
            # UI-valued expressions are completed with the component's deferred render task.
            return "null"
        if kind != "OP":
            raise ValueError(f"Unsupported expression: {kind}")
        args = [self.expr(v) for v in value["args"]]
        op = value["operator"]
        if op == "NOT":
            return f"(!({args[0]}))"
        if op == "LENGTH":
            return f"({args[0]}).length"
        if op == "IF":
            return f"({args[0]} ? {args[1]} : {args[2]})"
        if op == "CONCAT":
            return "(" + " + ".join(f"String({a})" for a in args) + ")"
        operators = {"AND": "&&", "OR": "||", "EQ": "===", "NE": "!==", "GT": ">", "GE": ">=", "LT": "<", "LE": "<=", "ADD": "+", "SUB": "-", "MUL": "*", "DIV": "/", "COALESCE": "??"}
        return "(" + f" {operators[op]} ".join(args) + ")"

    @staticmethod
    def _name(text: str, *, pascal: bool = False) -> str:
        clean = "".join(ch if ch.isidentifier() or ch.isdecimal() else " " for ch in text)
        words = re.findall(r"[^\W_]+", clean, flags=re.UNICODE)
        if not words:
            return "Component" if pascal else "value"
        first = words[0][:1].upper() + words[0][1:] if pascal else words[0][:1].lower() + words[0][1:]
        name = (first + "".join(w[:1].upper() + w[1:] for w in words[1:]))[:80]
        return name if name.isidentifier() else ("Component" if pascal else "value") + name

    @classmethod
    def _allocate_names(cls, rows: list[dict[str, Any]], reserved: set[str], *,
                        pascal: bool = False, casefold: bool = False) -> dict[str, str]:
        normalize = str.casefold if casefold else str
        used = {normalize(name) for name in reserved}
        bases = {r["id"]: cls._name(str(r.get("name", "")), pascal=pascal) for r in rows}
        counts: dict[str, int] = {}
        for base in bases.values():
            key = normalize(base)
            counts[key] = counts.get(key, 0) + 1
        names = {}
        for row in sorted(rows, key=lambda row: row["id"]):
            base = bases[row["id"]]
            suffix = re.sub(r"\W", "_", row["id"])
            name = base
            if counts[normalize(base)] > 1 or normalize(name) in used:
                name = f"{base}_{suffix}"
            while normalize(name) in used:
                name += "_" + suffix
            names[row["id"]] = name
            used.update(normalize(n) for n in (name, "set_" + name, "default_" + name,
                                               "runner_" + name, "start_" + name, "item_" + name))
        return names

    @classmethod
    def _component_names(cls, components: list[dict[str, Any]]) -> dict[str, str]:
        return cls._allocate_names(components, {
            "React", "createEffectRunner", "callApi", "con", "prn", "aux", "nul",
            *{f"com{i}" for i in range(10)}, *{f"lpt{i}" for i in range(10)},
        }, pascal=True, casefold=True)

    def _plan_symbols(self) -> None:
        reserved = set(
            "break case catch class const continue debugger default delete do else enum export extends false "
            "finally for function if import in instanceof new null return super switch this throw true try "
            "typeof var void while with yield let static implements interface package private protected public "
            "await async arguments eval input props payload event signal onCleanup isCurrent run console Error "
            "React createEffectRunner callApi".split()
        ) | set(self.component_names.values())
        self.symbols = dict(self.component_names)
        self.members = {}
        component_scopes = {}
        for component in self.ir["components"]:
            cid = component["id"]
            rows = [r for table in ("properties", "handlers", "effects", "events")
                    for r in self.ir[table] if r["component_id"] == cid]
            rows += self.data(cid, "INPUT")
            names = self._allocate_names(rows, reserved)
            self.symbols.update(names)
            component_scopes[cid] = reserved | set(names.values())
            for row in rows:
                if row["id"] in {d["id"] for d in self.data(cid, "INPUT")} or row.get("kind") == "CUSTOM":
                    self.members[row["id"]] = names[row["id"]]
            # Repeated UI nodes have named item bindings, separate from event/behavior names.
            self.symbols.update(self._allocate_names(
                [r for r in self.ir["ui"] if r["component_id"] == cid], component_scopes[cid]))
        for table in ("events", "handlers", "effects"):
            for owner in self.ir[table]:
                inputs, outputs = self.data(owner["id"], "INPUT"), self.data(owner["id"], "OUTPUT")
                for rows in (inputs, outputs):
                    self.members.update(self._allocate_names(rows, set()))
                # Local input aliases must not shadow captured parent state or functions.
                self.symbols.update(self._allocate_names(
                    inputs + outputs, component_scopes[owner["component_id"]]))

    def symbol(self, identifier: str) -> str:
        return self.symbols[identifier]

    def member(self, identifier: str) -> str:
        return self.members.get(identifier, self.symbol(identifier))

    def _contract_value(self, value: Any) -> Any:
        if isinstance(value, str) and value in self.rows:
            return self.symbols.get(value, self.rows[value].get("name", value))
        if isinstance(value, list):
            return [self._contract_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._contract_value(item) for key, item in value.items()}
        return value

    def _contract_comment(self, row: dict[str, Any]) -> str:
        """Preserve deterministic design facts beside the generated declaration."""
        facts = {key: self._contract_value(row[key]) for key in (
            "spec", "ui_root_id", "source_ui_id", "event_id", "handler_id", "handlers",
            "reads", "writes", "invokes", "emits", "target", "dependencies", "activation",
            "async_policy", "cleanup", "trigger", "ui_id", "handler_ids", "event_name", "arguments",
        ) if key in row and row[key] is not None}
        for direction in ("INPUT", "OUTPUT"):
            data = self.data(row["id"], direction)
            if data:
                facts[direction.lower()] = [{"name": self.members.get(d["id"], self.symbols.get(d["id"], d["name"])), "type": d["type"],
                                             "required": d["required"]} for d in data]
        return "\n".join("// Contract " + key + ": " + js(value)
                         for key, value in facts.items())

    def _placeholder(self, row: dict[str, Any], category: str) -> str:
        self.result.implementation_tasks.append({
            "entity_id": row["id"], "category": category, "name": row.get("name", ""),
            "file": f"frontend/src/components/{self.component_names[row['component_id']]}.tsx",
            "spec": row.get("spec", ""), "contract": row,
            "input_data": self.data(row["id"], "INPUT"), "output_data": self.data(row["id"], "OUTPUT"),
        })
        # Throwing fulfills every declared return type without fabricating successful business results.
        return self._contract_comment(row) + "\nthrow new Error(" + js("Not implemented: " + row["id"] + " " + row.get("name", "")) + ");"

    def _component(self, component: dict[str, Any]) -> None:
        cid = component["id"]
        name = self.component_names[cid]
        path = f"frontend/src/components/{name}.tsx"
        local = {table: [r for r in self.ir[table] if r.get("component_id") == cid] for table in ("properties", "ui", "events", "handlers", "effects")}
        imports = ['import * as React from "react";', 'import { createEffectRunner } from "../runtime/effects";', 'import { callApi } from "../api/client";']
        props = self.signature(cid, "INPUT")
        callbacks = [e for e in local["events"] if e["kind"] == "CUSTOM"]
        props += " & { " + "; ".join(f"{self.symbol(e['id'])}?: (payload: {self.signature(e['id'], 'OUTPUT')}) => void" for e in callbacks) + " }"
        lines = [*imports, "", self._contract_comment(component),
                 f"export function {name}(props: {props}) {{", "void [React, createEffectRunner, callApi, props];"]
        for d in self.data(cid, "INPUT"):
            n = self.symbol(d["id"])
            if d.get("default") is not None:
                if d["default"]["kind"] != "LITERAL":
                    raise ValueError(f"{d['id']}: input default must be a literal")
                lines.append(f"const default_{n} = React.useMemo<{ts_type(d['type'])}>(() => ({self.expr(d['default'])}), []);")
                lines.append(f"const {n} = props.{self.member(d['id'])} === undefined ? default_{n} : props.{self.member(d['id'])};")
            else:
                lines.append(f"const {n} = props.{self.member(d['id'])};")
        # Topologically order derived/initial expressions, with a useful cycle failure.
        pending = list(local["properties"])
        while pending:
            ready = [p for p in pending if not ({key for _, key in references(p.get("derive") or p.get("initial"))} & {r["id"] for r in pending})]
            if not ready:
                raise ValueError("Cyclic Property initial/derive dependencies")
            for p in ready:
                n, typ = self.symbol(p["id"]), ts_type(p["type"])
                if p["kind"] == "STATE":
                    lines.append(f"const [{n}, set_{n}] = React.useState<{typ}>(() => ({self.expr(p['initial'])}));")
                elif p["kind"] == "REF":
                    lines.append(f"const {n} = React.useRef<{typ}>({self.expr(p['initial'])});")
                    lines.append(f"const set_{n} = (value: {typ}) => {{ {n}.current = value; }};")
                else:
                    deps = ", ".join(self.expr({"kind": "REF", "ref_id": k, "path": []}) for k in sorted({key for _, key in references(p["derive"]) if key in self.members or self.rows[key].get("kind") in {"STATE", "REF", "DERIVED"}}))
                    lines.append(f"const {n} = React.useMemo<{typ}>(() => ({self.expr(p['derive'])}), [{deps}]);")
                pending.remove(p)
        for e in callbacks:
            lines.append(self._contract_comment(e))
            lines.append(f"const {self.symbol(e['id'])} = (payload: {self.signature(e['id'], 'OUTPUT')}) => props.{self.symbol(e['id'])}?.(payload);")
        for effect in local["effects"]:
            n = self.symbol(effect["id"])
            lines.extend([f"const runner_{n} = React.useRef(createEffectRunner({js(effect['async_policy'])}));",
                          f"React.useEffect(() => () => runner_{n}.current.cancelAll(), []);"])
        for row in [*local["effects"], *local["handlers"]]:
            n = self.symbol(row["id"])
            is_effect = row in local["effects"]
            bindings = [f"const {self.symbol(d['id'])} = input.{self.member(d['id'])}" + (f" === undefined ? {self.expr(d['default'])} : input.{self.member(d['id'])}" if d.get("default") is not None else "") + ";" for d in self.data(row["id"], "INPUT")]
            bindings.append("void [\n" + ",\n".join(["input", *[self.symbol(d["id"]) for d in self.data(row["id"], "INPUT")]]) + "\n];")
            body = self._placeholder(row, "effect" if is_effect else "handler")
            if is_effect:
                if row["kind"] == "REQUEST" and row["target"] not in self.routes:
                    self.result.warnings.append(
                        f"{row['id']}: unbound API target {row['target']}; generated an effect placeholder only.")
                lines.append(f"function start_{n}(input: {self.signature(row['id'], 'INPUT')}) {{ return runner_{n}.current.run(async ({{ signal, onCleanup, isCurrent }}): Promise<{self.signature(row['id'], 'OUTPUT')}> => {{\n" + "\n".join(bindings) + f"\nvoid [signal, onCleanup, isCurrent];\n{body}\n}}); }}")
                lines.append(f"function {n}(input: {self.signature(row['id'], 'INPUT')}) {{ return start_{n}(input).promise; }}")
            else:
                lines.append(f"async function {n}(input: {self.signature(row['id'], 'INPUT')}): Promise<{self.signature(row['id'], 'OUTPUT')}> {{\n" + "\n".join(bindings) + f"\n{body}\n}}")
        for event in local["events"]:
            if event["kind"] == "UI":
                body = self._placeholder(event, "event_payload")
                lines.append(f"function {self.symbol(event['id'])}(event: React.SyntheticEvent<HTMLElement>): {self.signature(event['id'], 'OUTPUT')} {{\nvoid event;\n{body}\n}}")
        self.result.implementation_tasks.append({
            "entity_id": cid, "category": "component_render", "name": component["name"], "file": path,
            "contract": component, "input_data": self.data(cid, "INPUT"), "ui": local["ui"],
            "events": local["events"], "effects": local["effects"],
            "description": "Implement the component body, child composition, bindings and effect activation from the design IR.",
        })
        for effect in local["effects"]:
            if effect["activation"] == "REACTIVE":
                deps = ", ".join(self.expr({"kind": "REF", "ref_id": key, "path": []}) for key in effect["dependencies"])
                lines.append(f"React.useEffect(() => {{\n// TODO: Implement reactive activation for {self.symbol(effect['id'])}.\n// Do not execute an unimplemented effect on mount.\n}}, [{deps}]);")
        # Keep declared capabilities available to the later implementation stage.
        # Retain those declarations without disabling the project's noUnused checks.
        available = [self.symbol(d["id"]) for d in self.data(cid, "INPUT")]
        available.extend(self.symbol(r["id"]) for table in ("properties", "handlers", "effects") for r in local[table])
        available.extend("set_" + self.symbol(p["id"]) for p in local["properties"] if p["kind"] != "DERIVED")
        available.extend(self.symbol(e["id"]) for e in local["events"])
        if available:
            lines.append("void [\n" + ",\n".join(available) + "\n];")
        for ui in local["ui"]:
            facts = {key: self._contract_value(ui[key]) for key in ("name", "kind", "element", "spec", "children", "component_ref",
                     "arguments", "callbacks", "condition", "repeat", "attributes", "text", "slot_data_id", "presentation")
                     if ui.get(key) not in (None, [], "")}
            lines.append("// UI " + self.symbol(ui["id"]) + ": " + js(facts))
        lines.extend(["// TODO: Implement the component UI and bindings from the contracts above.",
                      "return <React.Fragment />;", "}", ""])
        self.result.sources[path] = "\n".join(lines)
        ids = [component, *[r for rows in local.values() for r in rows]]
        ids.extend(d for d in self.ir["data"] if d["owner_id"] in {r["id"] for r in ids})
        self.result.bindings.extend({"entity_id": r["id"], "file": path, "component_symbol": name,
                                     "symbol": name if r["id"] == cid else None if r in local["ui"] else self.symbol(r["id"]),
                                     "requirement_ids": r["requirement_ids"]} for r in ids)

    def _api_source(self) -> None:
        routes = {key: {k: row[k] for k in ("method", "path", "input_source")} for key, row in self.routes.items()}
        self.result.sources["frontend/src/api/client.ts"] = (
            "const routes: Record<string, { method: string; path: string; input_source: string }> = " + js(routes) + ";\n"
            "export async function callApi<T>(id: string, input: Record<string, unknown>, signal?: AbortSignal): Promise<T> {\n"
            "const route = routes[id]; if (!route) throw new Error(`Unknown API ${id}`);\n"
            "let path = route.path; const rest = { ...input };\n"
            "path = path.replace(/:([A-Za-z0-9_]+)/g, (_, key: string) => { if (rest[key] == null) throw new Error(`Missing path parameter ${key}`); const value = encodeURIComponent(String(rest[key])); delete rest[key]; return value; });\n"
            "const init: RequestInit = { method: route.method }; if (signal) init.signal = signal;\n"
            "if (route.input_source === 'query') { const query = new URLSearchParams(); for (const [key, value] of Object.entries(rest)) if (value != null) query.set(key, String(value)); if (query.size) path += '?' + query.toString(); }\n"
            "else { init.headers = { 'content-type': 'application/json' }; init.body = JSON.stringify(rest); }\n"
            "const response = await fetch(path, init); const text = await response.text();\n"
            "if (!response.ok) throw Object.assign(new Error(text || response.statusText), { status: response.status });\n"
            "return (text ? JSON.parse(text) : undefined) as T;\n}\n"
        )


EFFECT_RUNTIME = '''type Scope = { signal: AbortSignal; onCleanup: (fn: () => void) => void; isCurrent: () => boolean };
export function createEffectRunner(policy: string) {
  let generation = 0;
  let tail: Promise<unknown> = Promise.resolve();
  const active = new Set<() => void>();
  return {
    run<T>(task: (scope: Scope) => Promise<T>) {
      const token = ++generation;
      const controller = new AbortController();
      const cleanup: Array<() => void> = [];
      let cancelled = false;
      const cancel = () => {
        if (cancelled) return;
        cancelled = true; controller.abort(); active.delete(cancel);
        for (const fn of cleanup.splice(0).reverse()) { try { fn(); } catch (error) { console.error(error); } }
      };
      if (policy === 'LATEST_WINS') for (const stop of [...active]) stop();
      active.add(cancel);
      const execute = async () => {
        if (cancelled) throw new DOMException('Cancelled', 'AbortError');
        try { return await task({ signal: controller.signal,
          onCleanup: fn => { if (cancelled) fn(); else cleanup.push(fn); },
          isCurrent: () => !cancelled && (policy !== 'LATEST_WINS' || token === generation) }); }
        finally { if (!cleanup.length) active.delete(cancel); }
      };
      const promise = policy === 'SERIAL' ? tail.then(execute, execute) : execute();
      tail = promise.catch(() => undefined);
      return { promise, cancel };
    },
    cancelAll() { ++generation; for (const cancel of [...active]) cancel(); }
  };
}
'''
