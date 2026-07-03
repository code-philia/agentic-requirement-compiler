import json
import re
from typing import Any


_QUOTE_RE = re.compile(r'"([^"\n]{1,160})"|\'([^\'\n]{1,160})\'')
_ROUTE_RE = re.compile(r'(?<![A-Za-z0-9_])(/(?:[A-Za-z0-9._~!$&\'()*+,;=:@%-]+/?)+)')
_BACKTICK_RE = re.compile(r'`([^`]{1,120})`')
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*(?:Id|Ids|Slug|Status|Key)\b")
_HTTP_ENDPOINT_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\b(?:\s+(?:request|endpoint|api|route|path))?(?:\s+to)?\s+['\"]?(/[^'\"\s,;]+)",
    re.IGNORECASE,
)
_AUTH_KEYWORDS = {
    "authenticated": "authenticated_state",
    "unauthenticated": "unauthenticated_access",
    "login": "login_flow",
    "log in": "login_flow",
    "logout": "logout_flow",
    "log out": "logout_flow",
    "register": "registration_flow",
    "session": "session_persistence",
    "protected": "protected_access",
    "redirect": "redirect_behavior",
}
_LIST_SUFFIX_RE = re.compile(r"(?:,\s*|\s+and\s+)")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        text = _safe_text(raw)
        if not text:
            continue
        lowered = text.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(text)
    return result


def _dedupe_objects(items: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = tuple(_safe_text(item.get(field, "")).casefold() for field in key_fields)
        if not any(key):
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _extract_quotes(text: str) -> list[str]:
    values: list[str] = []
    for match in _QUOTE_RE.finditer(text or ""):
        literal = match.group(1) or match.group(2) or ""
        literal = _safe_text(literal)
        if literal:
            values.append(literal)
    return _dedupe_strings(values)


def _normalize_route(value: str) -> str:
    route = _safe_text(value)
    if not route.startswith("/"):
        return route
    route = re.sub(r"/{2,}", "/", route)
    if len(route) > 1:
        route = route.rstrip("/")
    return route or "/"


def _extract_routes(text: str, source: str) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for route in _ROUTE_RE.findall(text or ""):
        normalized = _normalize_route(route)
        if not normalized.startswith("/"):
            continue
        routes.append(
            {
                "value": normalized,
                "surface": "api" if normalized.startswith("/api/") else "route",
                "source": source,
            }
        )
    return _dedupe_objects(routes, ("value", "surface"))


def _extract_api_endpoints(text: str, source: str) -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []
    for method, path in _HTTP_ENDPOINT_RE.findall(text or ""):
        endpoints.append(
            {
                "method": method.upper(),
                "path": _normalize_route(path),
                "source": source,
            }
        )
    for route in _extract_routes(text, source):
        if str(route.get("value", "")).startswith("/api/"):
            endpoints.append(
                {
                    "method": "",
                    "path": route["value"],
                    "source": source,
                }
            )
    return _dedupe_objects(endpoints, ("method", "path"))


def _extract_prefixed_quotes(text: str, source: str, kind: str, patterns: list[re.Pattern[str]]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for pattern in patterns:
        for match in pattern.finditer(text or ""):
            literal = _safe_text(match.group(1))
            if not literal:
                continue
            values.append({"text": literal, "kind": kind, "source": source})
    return values


def _extract_field_pairs(text: str, source: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    lowered = text.lower()
    labels: list[str] = []
    placeholders: list[str] = []

    field_markers = [
        "labeled inputs",
        "labelled inputs",
        "inputs",
        "input fields",
        "fields",
    ]
    placeholder_markers = [
        "with placeholders",
        "placeholders",
    ]

    for marker in field_markers:
        if marker not in lowered:
            continue
        start = lowered.find(marker)
        if start == -1:
            continue
        end = len(text)
        for stop in (" with placeholders", ". ", "\n", " plus ", " and a submit button", " and the button"):
            candidate = lowered.find(stop, start)
            if candidate != -1:
                end = min(end, candidate)
        labels.extend(_extract_quotes(text[start:end]))

    for marker in placeholder_markers:
        if marker not in lowered:
            continue
        start = lowered.find(marker)
        if start == -1:
            continue
        end = len(text)
        for stop in (". ", "\n", " plus ", " and a submit button", " and the button"):
            candidate = lowered.find(stop, start)
            if candidate != -1:
                end = min(end, candidate)
        placeholders.extend(_extract_quotes(text[start:end]))

    labels = _dedupe_strings(labels)
    placeholders = _dedupe_strings(placeholders)
    max_len = max(len(labels), len(placeholders))
    for index in range(max_len):
        label = labels[index] if index < len(labels) else ""
        placeholder = placeholders[index] if index < len(placeholders) else ""
        if not label and not placeholder:
            continue
        entries.append(
            {
                "label": label,
                "placeholder": placeholder,
                "source": source,
            }
        )

    inline_label_pattern = re.compile(r'into\s+"([^"]{1,120})"', re.IGNORECASE)
    for match in inline_label_pattern.finditer(text or ""):
        label = _safe_text(match.group(1))
        if not label:
            continue
        entries.append(
            {
                "label": label,
                "placeholder": "",
                "source": source,
            }
        )

    return _dedupe_objects(entries, ("label", "placeholder"))


def _classify_visible_literals(text: str, source: str) -> list[dict[str, Any]]:
    patterns_by_kind: dict[str, list[re.Pattern[str]]] = {
        "button_label": [
            re.compile(r'button(?:\s+\w+){0,3}\s+(?:labeled|labelled)\s+"([^"]{1,120})"', re.IGNORECASE),
            re.compile(r'cta(?:\s+\w+){0,3}\s+(?:labeled|labelled)\s+"([^"]{1,120})"', re.IGNORECASE),
        ],
        "link_label": [
            re.compile(r'link(?:\s+\w+){0,3}\s+(?:labeled|labelled)\s+"([^"]{1,120})"', re.IGNORECASE),
            re.compile(r'"([^"]{1,120})"\s+link', re.IGNORECASE),
            re.compile(r'navigation entry(?:\s+\w+){0,3}\s+(?:labeled|labelled)\s+"([^"]{1,120})"', re.IGNORECASE),
        ],
        "heading": [
            re.compile(r'(?:section|page|screen)\s+(?:titled|headed by)\s+"([^"]{1,120})"', re.IGNORECASE),
            re.compile(r'showing the\s+(?:section|page)\s+"([^"]{1,120})"', re.IGNORECASE),
        ],
        "action_label": [
            re.compile(r'action(?:\s+\w+){0,3}\s+(?:labeled|labelled)\s+"([^"]{1,120})"', re.IGNORECASE),
            re.compile(r'entry(?:\s+\w+){0,3}\s+(?:labeled|labelled)\s+"([^"]{1,120})"', re.IGNORECASE),
        ],
        "message": [
            re.compile(r'(?:message|feedback|error|validation message)(?:\s+\w+){0,3}\s+"([^"]{1,160})"', re.IGNORECASE),
            re.compile(r'such as\s+"([^"]{1,160})"', re.IGNORECASE),
        ],
    }

    visible: list[dict[str, Any]] = []
    for kind, patterns in patterns_by_kind.items():
        visible.extend(_extract_prefixed_quotes(text, source, kind, patterns))

    field_pairs = _extract_field_pairs(text, source)
    for item in field_pairs:
        label = _safe_text(item.get("label", ""))
        placeholder = _safe_text(item.get("placeholder", ""))
        if label:
            visible.append({"text": label, "kind": "field_label", "source": source})
        if placeholder:
            visible.append({"text": placeholder, "kind": "placeholder", "source": source})

    return _dedupe_objects(visible, ("text", "kind"))


def _extract_state_keys(text: str, source: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    if not re.search(r"(localstorage|sessionstorage|cookie|state key|storage key)", text or "", re.IGNORECASE):
        return values
    for literal in _extract_quotes(text):
        values.append({"key": literal, "source": source})
    return _dedupe_objects(values, ("key",))


def _extract_domain_terms(text: str, source: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for item in _BACKTICK_RE.findall(text or ""):
        literal = _safe_text(item)
        if literal:
            values.append({"term": literal, "source": source})
    for item in _IDENTIFIER_RE.findall(text or ""):
        literal = _safe_text(item)
        if literal:
            values.append({"term": literal, "source": source})
    return _dedupe_objects(values, ("term",))


def _extract_auth_flags(texts: list[str]) -> list[str]:
    flags: list[str] = []
    joined = "\n".join(texts).lower()
    for keyword, flag in _AUTH_KEYWORDS.items():
        if keyword in joined and flag not in flags:
            flags.append(flag)
    return flags


def _extract_rule_sentences(texts: list[str], positive_markers: tuple[str, ...]) -> list[str]:
    results: list[str] = []
    for text in texts:
        for segment in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
            sentence = _safe_text(segment)
            lowered = sentence.lower()
            if not sentence:
                continue
            if any(marker in lowered for marker in positive_markers):
                results.append(sentence)
    return _dedupe_strings(results)


def _build_scenario_contracts(node: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for scenario in node.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        scenario_name = _safe_text(scenario.get("name", ""))
        steps = scenario.get("steps") or []
        given: list[str] = []
        when: list[str] = []
        then: list[str] = []
        quoted_literals: list[str] = []
        routes: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            keyword = _safe_text(step.get("keyword") or step.get("type")).upper()
            content = _safe_text(step.get("content", ""))
            if keyword == "GIVEN":
                given.append(content)
            elif keyword == "WHEN":
                when.append(content)
            elif keyword == "THEN":
                then.append(content)
            if content:
                quoted_literals.extend(_extract_quotes(content))
                routes.extend(_extract_routes(content, f"scenario:{scenario_name or 'unnamed'}"))
        results.append(
            {
                "scenario_id": _safe_text(scenario.get("scenario_id") or scenario.get("id") or scenario_name),
                "name": scenario_name,
                "given": given,
                "when": when,
                "then": then,
                "quoted_literals": _dedupe_strings(quoted_literals),
                "routes": routes,
            }
        )
    return results


def _lint_contract(contract: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []

    form_fields = contract.get("signals", {}).get("form_fields") or []
    missing_label_placeholders = [
        item for item in form_fields
        if _safe_text(item.get("label", "")) and not _safe_text(item.get("placeholder", ""))
    ]
    if missing_label_placeholders and len(form_fields) > 1:
        warnings.append("Some extracted form fields have no paired placeholder. Keep labels authoritative and infer placeholder only when explicit.")

    placeholder_only = [
        item for item in form_fields
        if _safe_text(item.get("placeholder", "")) and not _safe_text(item.get("label", ""))
    ]
    if placeholder_only:
        warnings.append("Some extracted placeholders have no paired field label.")

    routes = contract.get("signals", {}).get("routes") or []
    duplicated_surfaces = [
        item for item in routes
        if str(item.get("surface", "")) == "api" and not str(item.get("value", "")).startswith("/api/")
    ]
    if duplicated_surfaces:
        warnings.append("Some API-like routes do not use an /api prefix. Confirm whether they are UI routes or API endpoints.")

    if not contract.get("signals", {}).get("visible_text") and not contract.get("signals", {}).get("routes"):
        warnings.append("No explicit visible text or route literal was extracted from this node.")

    return {"warnings": warnings, "errors": errors}


def _compile_single_node(
    node: dict[str, Any],
    parent_id: str,
    ancestor_ids: list[str],
) -> dict[str, Any]:
    req_id = _safe_text(node.get("id", "UNKNOWN_ID")) or "UNKNOWN_ID"
    children = node.get("children") or []
    children_ids = [
        _safe_text(child.get("id", ""))
        for child in children
        if isinstance(child, dict) and _safe_text(child.get("id", ""))
    ]
    dependencies = _dedupe_strings([_safe_text(item) for item in (node.get("dependencies") or [])])
    node_role = "leaf" if not children_ids else "non_leaf"
    node_type = _safe_text(node.get("type", "")) or ("ATOMIC" if node_role == "leaf" else "FOLDER")

    source_texts: list[tuple[str, str]] = []
    name = _safe_text(node.get("name", ""))
    description = _safe_text(node.get("description", ""))
    if name:
        source_texts.append(("name", name))
    if description:
        source_texts.append(("description", description))

    for index, scenario in enumerate(node.get("scenarios") or [], start=1):
        if not isinstance(scenario, dict):
            continue
        scenario_name = _safe_text(scenario.get("name", ""))
        if scenario_name:
            source_texts.append((f"scenario_name:{index}", scenario_name))
        for step_index, step in enumerate(scenario.get("steps") or [], start=1):
            if not isinstance(step, dict):
                continue
            content = _safe_text(step.get("content", ""))
            if content:
                source_texts.append((f"scenario_step:{index}.{step_index}", content))

    routes: list[dict[str, Any]] = []
    api_endpoints: list[dict[str, Any]] = []
    visible_text: list[dict[str, Any]] = []
    form_fields: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    state_keys: list[dict[str, Any]] = []
    domain_terms: list[dict[str, Any]] = []

    raw_texts = [text for _, text in source_texts]
    for source, text in source_texts:
        routes.extend(_extract_routes(text, source))
        api_endpoints.extend(_extract_api_endpoints(text, source))
        visible_text.extend(_classify_visible_literals(text, source))
        form_fields.extend(_extract_field_pairs(text, source))
        state_keys.extend(_extract_state_keys(text, source))
        domain_terms.extend(_extract_domain_terms(text, source))
        for item in _extract_prefixed_quotes(
            text,
            source,
            "message",
            [
                re.compile(r'(?:message|feedback|error|validation message)(?:\s+\w+){0,3}\s+"([^"]{1,160})"', re.IGNORECASE),
                re.compile(r'such as\s+"([^"]{1,160})"', re.IGNORECASE),
            ],
        ):
            messages.append(
                {
                    "text": item["text"],
                    "kind": item["kind"],
                    "source": item["source"],
                }
            )

    routes = _dedupe_objects(routes, ("value", "surface"))
    api_endpoints = _dedupe_objects(api_endpoints, ("method", "path"))
    visible_text = _dedupe_objects(visible_text, ("text", "kind"))
    form_fields = _dedupe_objects(form_fields, ("label", "placeholder"))
    messages = _dedupe_objects(messages, ("text", "kind"))
    state_keys = _dedupe_objects(state_keys, ("key",))
    domain_terms = _dedupe_objects(domain_terms, ("term",))

    auth_flags = _extract_auth_flags(raw_texts)
    must_statements = _extract_rule_sentences(raw_texts, ("must", "should", "needs to", "required to"))
    forbidden_shortcuts = _extract_rule_sentences(
        raw_texts,
        ("do not", "must not", "without relying on", "not from screenshot", "instead of fake", "not depend on"),
    )

    contract = {
        "schema_version": "arc.requirement_contract.node.v1",
        "req_id": req_id,
        "name": name,
        "node_type": node_type,
        "node_role": node_role,
        "lineage": {
            "parent_id": _safe_text(parent_id),
            "ancestor_ids": list(ancestor_ids),
            "children_ids": children_ids,
            "dependencies": dependencies,
        },
        "ownership": {
            "required_dependencies": dependencies,
            "required_children": children_ids,
        },
        "signals": {
            "routes": routes,
            "api_endpoints": api_endpoints,
            "visible_text": visible_text,
            "form_fields": form_fields,
            "messages": messages,
            "state_keys": state_keys,
            "domain_terms": domain_terms,
            "auth_flags": auth_flags,
        },
        "acceptance": {
            "scenario_names": _dedupe_strings(
                [_safe_text(scenario.get("name", "")) for scenario in (node.get("scenarios") or []) if isinstance(scenario, dict)]
            ),
            "must_statements": must_statements[:16],
            "forbidden_shortcuts": forbidden_shortcuts[:16],
        },
        "scenario_contracts": _build_scenario_contracts(node),
    }
    contract["lint"] = _lint_contract(contract)
    return contract


def compile_requirement_contract_bundle(requirement_tree: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(requirement_tree, dict):
        raise ValueError("Requirement contract compiler expects a requirement tree dictionary.")

    root_id = _safe_text(requirement_tree.get("id", ""))
    if not root_id:
        raise ValueError("Requirement contract compiler requires the root node to have an id.")

    contracts: dict[str, dict[str, Any]] = {}
    node_order: list[str] = []

    def walk(node: dict[str, Any], parent_id: str, ancestor_ids: list[str]) -> None:
        if not isinstance(node, dict):
            return
        req_id = _safe_text(node.get("id", ""))
        if not req_id:
            return
        contract = _compile_single_node(node=node, parent_id=parent_id, ancestor_ids=ancestor_ids)
        contracts[req_id] = contract
        node_order.append(req_id)
        next_ancestors = list(ancestor_ids) + [req_id]
        for child in node.get("children") or []:
            walk(child, req_id, next_ancestors)

    walk(requirement_tree, "", [])

    warning_count = sum(len((contract.get("lint") or {}).get("warnings") or []) for contract in contracts.values())
    error_count = sum(len((contract.get("lint") or {}).get("errors") or []) for contract in contracts.values())

    return {
        "schema_version": "arc.requirement_contract.bundle.v1",
        "root_id": root_id,
        "node_order": node_order,
        "contracts": contracts,
        "summary": {
            "node_count": len(node_order),
            "warning_count": warning_count,
            "error_count": error_count,
        },
    }

