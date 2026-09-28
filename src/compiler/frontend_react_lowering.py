"""Lower the seven-entity frontend IR without projecting it into Thin Frontend IR.

The compiler owns React structure and wiring. Models implement only one local
behavior or presentation contract at a time; they never select output paths.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logging import SynchronousLog
from .design_projection import project_api_contracts
from .frontend_generation_contracts import check_shape
from .frontend_workspace import references
from .model_client import StructuredModel, describe_model_error


def js(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def symbol(identifier: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", identifier) + "_" + hashlib.sha256(identifier.encode()).hexdigest()[:8]


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
        return "{ " + "; ".join(js(f["name"]) + ("" if f["required"] else "?") + ": " + ts_type(f["type"]) for f in value["fields"]) + " }"
    raise ValueError(f"Unsupported type: {kind}")


@dataclass
class ReactLoweringResult:
    sources: dict[str, str] = field(default_factory=dict)
    bindings: list[dict[str, Any]] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def report(self) -> dict[str, Any]:
        return {"status": "LOWERED" if self.ok else "FAILED", "errors": self.errors,
                "files": sorted(self.sources), "bindings": self.bindings}


class FrontendReactLowerer:
    def __init__(self, model: StructuredModel, artifact_root: Path) -> None:
        self.model = model
        self.log = SynchronousLog("FrontendReactLowerer", workspace_root=artifact_root.resolve().parent)

    def lower(self, ir: dict[str, Any], backend_design: dict[str, Any],
              backend_routes: dict[str, Any], backend_port: int) -> ReactLoweringResult:
        self.result = ReactLoweringResult()
        self.ir = ir
        self.rows: dict[str, dict[str, Any]] = {}
        self.apis = {r["id"]: r for r in project_api_contracts(backend_design)}
        self.routes = {r["module_id"]: r for r in backend_routes.get("routes", [])}
        self.fragments: dict[str, str] = {}
        self.rendering: set[str] = set()
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
            self._api_source()
            for component in ir["components"]:
                try:
                    self._component(component)
                except (ValueError, KeyError, TypeError, RecursionError) as exc:
                    self.result.errors.append(f"{component['id']}: {exc}")
            root = symbol(ir["root_component_id"])
            required = [d for d in self.data(ir["root_component_id"], "INPUT") if d["required"] and d.get("default") is None]
            if required:
                raise ValueError("Root has unsupplied required inputs: " + ", ".join(d["id"] for d in required))
            self.result.sources["frontend/src/App.tsx"] = f'import {{ {root} }} from "./components/{root}";\nexport default function App() {{ return <{root} />; }}\n'
            self.result.sources["frontend/src/runtime/effects.ts"] = EFFECT_RUNTIME
            self.result.sources["frontend/vite.config.ts"] = (
                'import { defineConfig } from "vite";\nimport react from "@vitejs/plugin-react";\n'
                'import tailwindcss from "@tailwindcss/vite";\n'
                f'const proxy = {{ "/api": {{ target: "http://127.0.0.1:{int(backend_port)}", changeOrigin: true }} }};\n'
                'export default defineConfig({ plugins: [react(), tailwindcss()], server: { proxy }, preview: { proxy } });\n'
            )
        except (ValueError, KeyError, TypeError) as exc:
            self.result.errors.append(str(exc))
        return self.result

    def data(self, owner: str, direction: str) -> list[dict[str, Any]]:
        return [r for r in self.ir["data"] if r["owner_id"] == owner and r["direction"] == direction]

    def signature(self, owner: str, direction: str) -> str:
        return "{ " + "; ".join(symbol(d["id"]) + ("" if d["required"] and d.get("default") is None else "?") + ": " + ts_type(d["type"]) for d in self.data(owner, direction)) + " }"

    def args(self, values: list[dict[str, Any]]) -> str:
        return "{ " + ", ".join(symbol(v["parameter_id"]) + ": " + self.expr(v["value"]) for v in values) + " }"

    def expr(self, value: dict[str, Any] | None) -> str:
        if value is None:
            return "undefined"
        kind = value["kind"]
        if kind == "LITERAL":
            return js(value["value"])
        if kind in {"REF", "ITEM"}:
            key = value["ref_id"] if kind == "REF" else value["ui_id"]
            row = self.rows[key]
            base = symbol(key) if kind == "REF" else "item_" + symbol(key)
            if kind == "REF" and row.get("kind") == "REF":
                base += ".current"
            return base + "".join("[" + js(p) + "]" for p in value.get("path", []))
        if kind == "UI_REF":
            return self.render(value["ui_id"], set())
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

    def _fragment(self, row: dict[str, Any], contract: str, scope: Any) -> str:
        schema = {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"], "additionalProperties": False}
        payload = {"entity": row, "contract": contract, "scope": scope}
        record: dict[str, Any] = {"entity_id": row["id"], "input": payload, "attempts": []}
        self.result.batches.append(record)
        error = ""
        for attempt in range(2):
            request = {**payload, "repair": error[:1200]}
            if len(js(request)) > 24000:
                record["status"] = "FAILED"
                record["attempts"].append({"error": "Local implementation context exceeds 24000 chars"})
                raise ValueError(f"{row['id']}: local implementation context exceeds 24000 chars")
            self.log.info(f"MODEL_REQUEST phase=frontend_lowering task={row['id']} attempt={attempt + 1}/2 input_chars={len(js(request))}")
            try:
                output = self.model.generate_json(
                    schema_name="frontend_local_implementation", output_schema=schema, input_payload=request,
                    instructions="Implement precisely one React/TypeScript fragment using the supplied contract and exact compiler symbols. Return {code: string} only. No markdown, imports, exports, hooks, dependencies, invented API endpoints, TODOs or placeholders. Do not change the IR or component tree. Handle success/failure according to spec. Use existing browser APIs and supplied bindings only. Keep code concise (at most 8000 characters).",
                )
                check_shape(output, schema)
                code = output["code"].strip()
                if not code or len(code) > 8000 or "```" in code or re.search(r"\b(import|export)\s|\bTODO\b", code):
                    raise ValueError("Return a nonempty local fragment <=8000 chars, without module declarations or placeholders")
                record["attempts"].append({"output": output})
                record["status"] = "GENERATED"
                self.log.info(f"MODEL_APPLIED phase=frontend_lowering task={row['id']} output_chars={len(code)}")
                return code
            except Exception as exc:
                error = describe_model_error(exc)
                record["attempts"].append({"error": error})
                self.log.info(f"MODEL_{'RETRY' if attempt == 0 else 'FAILED'} phase=frontend_lowering task={row['id']} error={error}")
        record["status"] = "FAILED"
        raise ValueError(f"{row['id']}: {error}")

    def _scope(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        ids = set(row.get("reads", [])) | set(row.get("writes", [])) | set(row.get("dependencies", []))
        ids.update(key for _, key in references(row))
        result = []
        for key in sorted(ids):
            item = self.rows.get(key)
            if item is None or key == row["id"]:
                continue
            if item.get("type"):
                result.append({"id": key, "symbol": symbol(key) + (".current" if item.get("kind") == "REF" else ""),
                               "type": ts_type(item["type"]), "spec": item.get("spec", ""),
                               "setter": "set_" + symbol(key) if key in row.get("writes", []) else None})
        return result

    def _component(self, component: dict[str, Any]) -> None:
        cid = component["id"]
        name = symbol(cid)
        path = f"frontend/src/components/{name}.tsx"
        local = {table: [r for r in self.ir[table] if r.get("component_id") == cid] for table in ("properties", "ui", "events", "handlers", "effects")}
        imports = ['import * as React from "react";', 'import { createEffectRunner } from "../runtime/effects";', 'import { callApi } from "../api/client";']
        for child in sorted({u["component_ref"] for u in local["ui"] if u["kind"] == "COMPONENT"}):
            if child != cid:
                imports.append(f'import {{ {symbol(child)} }} from "./{symbol(child)}";')
        props = self.signature(cid, "INPUT")
        callbacks = [e for e in local["events"] if e["kind"] == "CUSTOM"]
        props += " & { " + "; ".join(f"{symbol(e['id'])}?: (payload: {self.signature(e['id'], 'OUTPUT')}) => void" for e in callbacks) + " }"
        lines = [*imports, f"export function {name}(props: {props}) {{", "void [React, createEffectRunner, callApi, props];"]
        for d in self.data(cid, "INPUT"):
            n = symbol(d["id"])
            if d.get("default") is not None:
                if d["default"]["kind"] != "LITERAL":
                    raise ValueError(f"{d['id']}: input default must be a literal")
                lines.append(f"const default_{n} = React.useMemo<{ts_type(d['type'])}>(() => ({self.expr(d['default'])}), []);")
                lines.append(f"const {n} = props.{n} === undefined ? default_{n} : props.{n};")
            else:
                lines.append(f"const {n} = props.{n};")
        # Topologically order derived/initial expressions, with a useful cycle failure.
        pending = list(local["properties"])
        while pending:
            ready = [p for p in pending if not ({key for _, key in references(p.get("derive") or p.get("initial"))} & {r["id"] for r in pending})]
            if not ready:
                raise ValueError("Cyclic Property initial/derive dependencies")
            for p in ready:
                n, typ = symbol(p["id"]), ts_type(p["type"])
                if p["kind"] == "STATE":
                    lines.append(f"const [{n}, set_{n}] = React.useState<{typ}>(() => ({self.expr(p['initial'])}));")
                elif p["kind"] == "REF":
                    lines.append(f"const {n} = React.useRef<{typ}>({self.expr(p['initial'])});")
                    lines.append(f"const set_{n} = (value: {typ}) => {{ {n}.current = value; }};")
                else:
                    deps = ", ".join(self.expr({"kind": "REF", "ref_id": k, "path": []}) for k in sorted({key for _, key in references(p["derive"])}))
                    lines.append(f"const {n} = React.useMemo<{typ}>(() => ({self.expr(p['derive'])}), [{deps}]);")
                pending.remove(p)
        for e in callbacks:
            lines.append(f"const {symbol(e['id'])} = (payload: {self.signature(e['id'], 'OUTPUT')}) => props.{symbol(e['id'])}?.(payload);")
        for effect in local["effects"]:
            n = symbol(effect["id"])
            lines.extend([f"const runner_{n} = React.useRef(createEffectRunner({js(effect['async_policy'])}));",
                          f"React.useEffect(() => () => runner_{n}.current.cancelAll(), []);"])
        for row in [*local["effects"], *local["handlers"]]:
            n = symbol(row["id"])
            is_effect = row in local["effects"]
            bindings = [f"const {symbol(d['id'])} = input.{symbol(d['id'])}" + (f" === undefined ? {self.expr(d['default'])} : input.{symbol(d['id'])}" if d.get("default") is not None else "") + ";" for d in self.data(row["id"], "INPUT")]
            bindings.append("void [input" + "".join(", " + symbol(d["id"]) for d in self.data(row["id"], "INPUT")) + "];")
            scope = {"values": self._scope(row), "inputs": self.data(row["id"], "INPUT"), "outputs": self.data(row["id"], "OUTPUT"), "symbols": {d["id"]: symbol(d["id"]) for direction in ("INPUT", "OUTPUT") for d in self.data(row["id"], direction)}}
            contract = f"Async function BODY only. Return output object of type {self.signature(row['id'], 'OUTPUT')}; return {{}} when no outputs. Input values are declared for you."
            if is_effect:
                scope["external"] = {"api": self.apis.get(row.get("target")), "route": self.routes.get(row.get("target"))}
                if row["kind"] == "REQUEST" and row["target"] not in self.routes:
                    raise ValueError(f"{row['id']}: unknown API target {row['target']}")
                contract += " Use onCleanup(() => ...) for all cleanup, signal for cancellation. Set state only through supplied set_* functions; writes are guarded by compiler against stale results. Backend call: await callApi<T>(exact API id, input object, signal). Never write REF.current directly. NOT_ASYNC must not await. For subscriptions and timers register and return promptly; onCleanup releases the resource."
                if row["kind"] == "DOM":
                    scope["dom_targets"] = [{"id": u["id"], "element": u["element"], "spec": u["spec"], "selector": '[data-arc-ui="' + u["id"] + '"]'} for u in local["ui"] if u["kind"] == "ELEMENT"]
                guards = []
                for key in row["writes"]:
                    p = self.rows[key]
                    typ = ts_type(p["type"])
                    n2 = symbol(key)
                    guards.append(f"const set_{n2} = (value: {typ}" + (f" | ((previous: {typ}) => {typ})" if p["kind"] == "STATE" else "") + f") => {{ if (isCurrent()) write_{n2}(value); }};")
                    lines.append(f"const write_{n2} = set_{n2};" if not any(f"const write_{n2} =" in l for l in lines) else "")
                guards.append("void [signal, onCleanup, isCurrent" + "".join(", set_" + symbol(key) for key in row["writes"]) + "];")
                body = self._fragment(row, contract, scope)
                lines.append(f"function start_{n}(input: {self.signature(row['id'], 'INPUT')}) {{ return runner_{n}.current.run(async ({{ signal, onCleanup, isCurrent }}): Promise<{self.signature(row['id'], 'OUTPUT')}> => {{\n" + "\n".join(bindings + guards) + f"\n{body}\n}}); }}")
                lines.append(f"function {n}(input: {self.signature(row['id'], 'INPUT')}) {{ return start_{n}(input).promise; }}")
            else:
                scope["calls"] = [{"symbol": symbol(v["effect_id"]), "arguments": self.args(v["arguments"]), "return_type": self.signature(v["effect_id"], "OUTPUT")} for v in row["invokes"]]
                scope["emits"] = [{"symbol": symbol(v["event_id"]), "arguments": self.args(v["arguments"])} for v in row["emits"]]
                contract += " Invoke/emits list is permission, not unconditional execution. Use supplied argument expressions, branch and order according to spec. Effects return promises of output objects keyed by Data symbols: bind any referenced effect output from the awaited result before using it. Never write REF.current directly; use its setter."
                body = self._fragment(row, contract, scope)
                lines.append(f"async function {n}(input: {self.signature(row['id'], 'INPUT')}): Promise<{self.signature(row['id'], 'OUTPUT')}> {{\n" + "\n".join(bindings) + f"\n{body}\n}}")
        for event in local["events"]:
            if event["kind"] == "UI":
                ui = self.rows[event["ui_id"]]
                contract = f"Synchronous function BODY. Native React event is named event (React.SyntheticEvent<HTMLElement>). Narrow event.currentTarget as needed for {ui['element']}. Apply preventDefault/stopPropagation per spec. Return payload type {self.signature(event['id'], 'OUTPUT')}; no handler invocation here."
                self.fragments[event["id"]] = self._fragment(event, contract, {"outputs": self.data(event["id"], "OUTPUT"), "symbols": {d["id"]: symbol(d["id"]) for d in self.data(event["id"], "OUTPUT")}})
        for ui in local["ui"]:
            if ui.get("presentation") and ui["kind"] == "ELEMENT":
                self.fragments[ui["id"]] = self._fragment(
                    {k: ui[k] for k in ("id", "element", "presentation", "spec")},
                    'Return one JavaScript object EXPRESSION containing only className and/or style (React.CSSProperties). Use Tailwind 4 literal classes or inline styles. No behavior, children, text, or event attributes.',
                    {"component_spec": component["spec"]},
                )
        for effect in local["effects"]:
            if effect["activation"] == "REACTIVE":
                deps = ", ".join(self.expr({"kind": "REF", "ref_id": key, "path": []}) for key in effect["dependencies"])
                lines.append(f"React.useEffect(() => {{ const run = start_{symbol(effect['id'])}({self.args(effect['arguments'])}); void run.promise.catch(console.error); return run.cancel; }}, [{deps}]);")
        # Contracts can declare optional values/capabilities unused by one local implementation.
        # Retain those declarations without disabling the project's noUnused checks.
        available = [symbol(d["id"]) for d in self.data(cid, "INPUT")]
        available.extend(symbol(r["id"]) for table in ("properties", "handlers", "effects") for r in local[table])
        available.extend("set_" + symbol(p["id"]) for p in local["properties"] if p["kind"] != "DERIVED")
        available.extend(symbol(e["id"]) for e in callbacks)
        lines.append("void [" + ", ".join(available) + "];")
        lines.extend([f"return ({self.render(component['ui_root_id'], set())});", "}", ""])
        self.result.sources[path] = "\n".join(lines)
        ids = [component, *[r for rows in local.values() for r in rows]]
        ids.extend(d for d in self.ir["data"] if d["owner_id"] in {r["id"] for r in ids})
        self.result.bindings.extend({"entity_id": r["id"], "file": path, "component_symbol": name,
                                     "symbol": None if r in local["ui"] or (r in local["events"] and r["kind"] == "UI") else symbol(r["id"]),
                                     "requirement_ids": r["requirement_ids"]} for r in ids)

    def render(self, key: str, ancestors: set[str]) -> str:
        if key in self.rendering:
            raise ValueError(f"Cyclic UI reference: {key}")
        self.rendering.add(key)
        try:
            return self._render(key, ancestors)
        finally:
            self.rendering.remove(key)

    def _render(self, key: str, ancestors: set[str]) -> str:
        if key in ancestors:
            raise ValueError(f"Cyclic UI children: {key}")
        u = self.rows[key]
        ancestors = ancestors | {key}
        children = [self.render(k, ancestors) for k in u["children"]]
        kind = u["kind"]
        if kind == "TEXT":
            value = self.expr(u["text"])
        elif kind == "SLOT":
            value = symbol(u["slot_data_id"])
        elif kind == "FRAGMENT":
            value = "React.createElement(React.Fragment, null" + "".join(", " + c for c in children) + ")"
        else:
            attrs = []
            if kind == "COMPONENT":
                tag = symbol(u["component_ref"])
                attrs.extend(symbol(a["parameter_id"]) + ": " + self.expr(a["value"]) for a in u["arguments"])
                for cb in u["callbacks"]:
                    declarations = " ".join(f"const {symbol(d['id'])} = payload.{symbol(d['id'])}; void {symbol(d['id'])};" for d in self.data(cb["event_id"], "OUTPUT")) + " void payload;"
                    attrs.append(f"{symbol(cb['event_id'])}: (payload: {self.signature(cb['event_id'], 'OUTPUT')}) => {{ {declarations} void {symbol(cb['handler_id'])}({self.args(cb['arguments'])}).catch(console.error); }}")
            else:
                tag = js(u["element"])
                attrs.append('"data-arc-ui": ' + js(key))
                if key in self.fragments:
                    attrs.append("...(" + self.fragments[key] + ")")
                attrs.extend(js({"class": "className", "for": "htmlFor", "tabindex": "tabIndex"}.get(a["name"], a["name"])) + ": " + self.expr(a["value"]) for a in u["attributes"])
                for e in self.ir["events"]:
                    if e.get("ui_id") != key:
                        continue
                    event_name = e["event_name"]
                    event_name = event_name if event_name.startswith("on") else "on" + {"dblclick": "DoubleClick", "keydown": "KeyDown", "keyup": "KeyUp", "mouseenter": "MouseEnter", "mouseleave": "MouseLeave", "pointerdown": "PointerDown", "focusout": "Blur"}.get(event_name, event_name[:1].upper() + event_name[1:])
                    declarations = " ".join(f"const {symbol(d['id'])} = payload.{symbol(d['id'])}; void {symbol(d['id'])};" for d in self.data(e["id"], "OUTPUT")) + " void payload;"
                    attrs.append(f"{js(event_name)}: (event: React.SyntheticEvent<HTMLElement>) => {{ void event; const payload = ((): {self.signature(e['id'], 'OUTPUT')} => {{ {self.fragments[e['id']]} }})(); {declarations} void {symbol(e['handler_id'])}({self.args(e['arguments'])}).catch(console.error); }}")
            value = f"React.createElement({tag}, {{ " + ", ".join(attrs) + " }" + "".join(", " + c for c in children) + ")"
        if u.get("condition") is not None:
            value = f"({self.expr(u['condition'])} ? {value} : null)"
        if u.get("repeat"):
            repeat = u["repeat"]
            value = f"({self.expr(repeat['source'])}).map((item_{symbol(key)}) => {{ void item_{symbol(key)}; return React.createElement(React.Fragment, {{ key: {self.expr(repeat['key'])} }}, {value}); }})"
        return value

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
