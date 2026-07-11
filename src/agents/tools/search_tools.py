import os
import re
import json
import glob as glob_module
import itertools
import aiofiles
from runtime_sdk import get_runtime
from utils import get_abs_path


def _store():
    return get_runtime().traceability

_SKIP_DIRS = {
    ".git", ".arc", ".gradle", "build", ".idea",
    "node_modules", ".venv", "venv", "dist", "out", "coverage",
    "__pycache__", "target", ".next", ".nuxt", ".cache", ".turbo",
    ".parcel-cache", "tmp", "temp",
}
_SKIP_FILES = {
    ".gitignore", ".DS_Store", "Thumbs.db", "package-lock.json",
    "yarn.lock", "pnpm-lock.yaml", "composer.lock", "Gemfile.lock",
    ".npmrc", ".yarnrc",
}


def _should_skip_path(file_path: str, root_dir: str) -> bool:
    rel_path = os.path.relpath(file_path, root_dir)
    parts = rel_path.replace("\\", "/").split("/")
    if any(part in _SKIP_DIRS for part in parts[:-1]):
        return True
    return parts[-1] in _SKIP_FILES


def _normalize_rel_text(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").lower()


def _mentions_skipped_area(value: str) -> bool:
    normalized = _normalize_rel_text(value)
    return any(
        normalized == skip_dir
        or normalized.startswith(f"{skip_dir}/")
        or f"/{skip_dir}/" in normalized
        for skip_dir in _SKIP_DIRS
    )


def _is_broad_root_glob(pattern: str, path: str | None) -> bool:
    normalized_pattern = _normalize_rel_text(pattern)
    normalized_path = _normalize_rel_text(path or ".")
    if normalized_path not in {"", ".", "./"}:
        return False
    broad_patterns = {
        "**/*.js", "**/*.jsx", "**/*.ts", "**/*.tsx", "**/*.json",
        "**/*.py", "**/*.java", "**/*.md", "**/package.json",
        "**/vite.config.*", "**/vitest.config.*", "**/playwright.config.*",
    }
    return normalized_pattern in broad_patterns


def _reject_low_value_search(path: str | None, pattern: str, glob: str | None = None) -> str | None:
    normalized_path = _normalize_rel_text(path or ".")
    normalized_pattern = _normalize_rel_text(pattern)
    normalized_glob = _normalize_rel_text(glob or "")

    if _mentions_skipped_area(normalized_path):
        return (
            "Rejected search inside low-value generated/dependency directories. "
            "Do not explore node_modules, build outputs, caches, or virtual environments."
        )
    if _mentions_skipped_area(normalized_pattern) or _mentions_skipped_area(normalized_glob):
        return (
            "Rejected search pattern targeting low-value generated/dependency directories. "
            "Do not explore node_modules, build outputs, caches, or virtual environments."
        )
    if _is_broad_root_glob(normalized_pattern, normalized_path):
        return (
            "Rejected broad workspace-root glob. "
            "Use the existing <project_structure> context first and narrow the search to a known subtree such as backend/, frontend/, src/, or app/."
        )
    if normalized_path in {"", ".", "./"} and not normalized_glob:
        return (
            "Rejected unconstrained workspace-root content search. "
            "Narrow the path to a relevant subtree or provide a restrictive glob."
        )
    return None


def _expand_brace_pattern(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern or "")
    if not match:
        return [pattern]
    options = [item.strip() for item in match.group(1).split(",") if item.strip()]
    if not options:
        return [pattern]
    prefix = pattern[:match.start()]
    suffix = pattern[match.end():]
    expanded_suffixes = _expand_brace_pattern(suffix)
    results: list[str] = []
    for option, expanded_suffix in itertools.product(options, expanded_suffixes):
        results.append(prefix + option + expanded_suffix)
    return results


async def glob_impl(pattern: str, path: str = None) -> str:
    """
    Fast file pattern matching using glob patterns.

    Args:
        pattern: Glob pattern (e.g., "**/*.js", "src/**/*.ts")
        path: Directory to search in (defaults to current working directory)

    Returns:
        Newline-separated list of matching file paths (max 100 files)
    """
    try:
        rejection = _reject_low_value_search(path, pattern)
        if rejection:
            return rejection

        search_dir = get_abs_path(path) if path else get_abs_path(".")

        if not os.path.exists(search_dir):
            return f"Error: Directory not found: {path or '.'}"

        if not os.path.isdir(search_dir):
            return f"Error: Path is not a directory: {path or '.'}"

        # Use glob with recursive support; expand simple brace groups like *.{ts,tsx}
        matches: list[str] = []
        for expanded_pattern in _expand_brace_pattern(pattern):
            full_pattern = os.path.join(search_dir, expanded_pattern)
            matches.extend(glob_module.glob(full_pattern, recursive=True))
        matches = list(dict.fromkeys(matches))

        # Filter out directories plus skipped build/dependency files
        files = [
            match for match in matches
            if os.path.isfile(match) and not _should_skip_path(match, search_dir)
        ]

        # Sort by modification time (newest first)
        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

        # Limit to 100 results
        max_results = 100
        truncated = len(files) > max_results
        files = files[:max_results]

        # Convert to relative paths for readability
        base_dir = get_abs_path(".")
        relative_files = []
        for f in files:
            try:
                rel = os.path.relpath(f, base_dir)
                relative_files.append(rel)
            except ValueError:
                # If relpath fails (different drives on Windows), use absolute
                relative_files.append(f)

        if not relative_files:
            return (
                "No files found\n"
                "Hint: Prefer the existing <project_structure> context and only probe directories that are already known to exist."
            )

        result = "\n".join(relative_files)
        if truncated:
            result += "\n(Results are truncated. Consider using a more specific path or pattern.)"

        return result

    except Exception as e:
        return f"Glob search error: {str(e)}"

async def grep_impl(
    pattern: str,
    path: str = None,
    glob: str = None,
    output_mode: str = "files_with_matches",
    context: int = None,
    case_insensitive: bool = False,
    head_limit: int = 250,
    offset: int = 0,
    multiline: bool = False
) -> str:
    """
    Search for a regex pattern in file contents using ripgrep-like behavior.

    Args:
        pattern: Regular expression pattern to search for
        path: File or directory to search in (defaults to current directory)
        glob: Glob pattern to filter files (e.g., "*.js", "**/*.tsx")
        output_mode: "content" (matching lines), "files_with_matches" (file paths), "count" (match counts)
        context: Number of lines to show before and after each match (content mode only)
        case_insensitive: Case insensitive search
        head_limit: Limit output to first N results (default 250, 0 for unlimited)
        offset: Skip first N results before applying head_limit
        multiline: Enable multiline mode where . matches newlines

    Returns:
        Search results formatted according to output_mode
    """
    try:
        rejection = _reject_low_value_search(path, pattern, glob)
        if rejection:
            return rejection

        search_dir = get_abs_path(path) if path else get_abs_path(".")

        if not os.path.exists(search_dir):
            return f"Error: Path not found: {path or '.'}"

        # Compile regex pattern
        flags = re.IGNORECASE if case_insensitive else 0
        if multiline:
            flags |= re.DOTALL | re.MULTILINE
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Error: Invalid regex pattern: {str(e)}"

        # Determine file extensions to search
        if glob:
            # Simple glob to extension mapping
            file_extensions = []
            if "*." in glob:
                ext = glob.split("*.")[-1].split(")")[0].split(",")[0]
                file_extensions.append(f".{ext}")
            else:
                file_extensions = ['.py', '.js', '.ts', '.java', '.jsx', '.tsx', '.yaml', '.yml', '.json', '.md', '.txt', '.xml', '.html', '.css', '.sh', '.bat']
        else:
            file_extensions = ['.py', '.js', '.ts', '.java', '.jsx', '.tsx', '.yaml', '.yml', '.json', '.md', '.txt', '.xml', '.html', '.css', '.sh', '.bat']

        results = []
        file_matches = {}  # file_path -> list of (line_num, line_content)
        file_counts = {}   # file_path -> match_count

        # Walk directory tree
        if os.path.isfile(search_dir):
            files_to_search = [search_dir]
        else:
            files_to_search = []
            for root, dirs, files in os.walk(search_dir):
                dirs[:] = [directory for directory in dirs if directory not in _SKIP_DIRS]

                for file in files:
                    file_path = os.path.join(root, file)
                    if file in _SKIP_FILES or _should_skip_path(file_path, search_dir):
                        continue
                    if any(file.endswith(ext) for ext in file_extensions):
                        files_to_search.append(file_path)

        # Search files
        for file_path in files_to_search:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    if multiline:
                        content = f.read()
                        if regex.search(content):
                            file_matches[file_path] = []
                            file_counts[file_path] = len(regex.findall(content))
                            # For multiline, store the whole content
                            for match in regex.finditer(content):
                                line_num = content[:match.start()].count('\n') + 1
                                file_matches[file_path].append((line_num, match.group(0)))
                    else:
                        lines = f.readlines()
                        matches_in_file = []
                        for i, line in enumerate(lines):
                            if regex.search(line):
                                matches_in_file.append((i + 1, line.rstrip()))

                        if matches_in_file:
                            file_matches[file_path] = matches_in_file
                            file_counts[file_path] = len(matches_in_file)
            except Exception:
                continue

        # Convert to relative paths
        base_dir = get_abs_path(".")
        relative_file_matches = {}
        relative_file_counts = {}
        for file_path in file_matches:
            try:
                rel = os.path.relpath(file_path, base_dir)
            except ValueError:
                rel = file_path
            relative_file_matches[rel] = file_matches[file_path]
            relative_file_counts[rel] = file_counts[file_path]

        # Format output based on mode
        if output_mode == "files_with_matches":
            all_files = sorted(relative_file_matches.keys())
            # Apply offset and limit
            effective_limit = None if head_limit == 0 else head_limit
            if effective_limit:
                all_files = all_files[offset:offset + effective_limit]
            else:
                all_files = all_files[offset:]

            if not all_files:
                return "No matches found."

            result = "\n".join(all_files)
            if effective_limit and len(relative_file_matches) - offset > effective_limit:
                result += f"\n(Results truncated at {effective_limit} files. Use offset parameter to see more.)"
            return result

        elif output_mode == "count":
            all_counts = [(f, relative_file_counts[f]) for f in sorted(relative_file_counts.keys())]
            # Apply offset and limit
            effective_limit = None if head_limit == 0 else head_limit
            if effective_limit:
                all_counts = all_counts[offset:offset + effective_limit]
            else:
                all_counts = all_counts[offset:]

            if not all_counts:
                return "No matches found."

            result_lines = [f"{count}:{file_path}" for file_path, count in all_counts]
            result = "\n".join(result_lines)
            if effective_limit and len(relative_file_counts) - offset > effective_limit:
                result += f"\n(Results truncated at {effective_limit} files. Use offset parameter to see more.)"
            return result

        elif output_mode == "content":
            all_lines = []
            for file_path in sorted(relative_file_matches.keys()):
                matches = relative_file_matches[file_path]
                for line_num, line_content in matches:
                    all_lines.append(f"{file_path}:{line_num}: {line_content}")

            # Apply offset and limit
            effective_limit = None if head_limit == 0 else head_limit
            if effective_limit:
                all_lines = all_lines[offset:offset + effective_limit]
            else:
                all_lines = all_lines[offset:]

            if not all_lines:
                return "No matches found."

            result = "\n".join(all_lines)
            if effective_limit and len(all_lines) >= effective_limit:
                result += f"\n(Results truncated at {effective_limit} lines. Use offset parameter to see more.)"
            return result

        else:
            return f"Error: Invalid output_mode '{output_mode}'. Must be 'content', 'files_with_matches', or 'count'."

    except Exception as e:
        return f"Grep search error: {str(e)}"

async def get_node_relations_impl(node_id: str) -> str:
    """
    Get the parent and children nodes for a given requirement node, along with their designed interfaces.
    """
    try:
        store = _store()
        node_row = store.get_requirement(node_id)
        if not node_row:
            return f"Requirement node '{node_id}' not found in database."
            
        parent_id = node_row.get("parent_id")
        children_ids = node_row.get("children_ids") or []
            
        result = f"### Relational Context for Node [{node_id}]\n\n"
        
        # Helper to fetch node details + interfaces
        def fetch_node_details(n_id, label):
            req = store.get_requirement(n_id)
            if not req:
                return ""
            
            res = f"#### {label}: [{n_id}]\n"
            description = str(req.get("description", "") or "")
            res += f"Description: {description[:200]}...\n"
            
            ifaces = store.list_interfaces(req_id=n_id)
            
            if ifaces:
                res += "Interfaces:\n"
                for iface in ifaces:
                    res += f"  - ID: {iface['interface_id']} (Type: {iface['type']})\n"
                    if iface.get("file_path"):
                        res += f"    Path: `{iface['file_path']}`\n"
                    if iface.get("first_line"):
                        res += f"    Signature: `{iface['first_line']}`\n"
            else:
                res += "No interfaces designed yet for this node.\n"
            return res + "\n"

        # Fetch Parent
        if parent_id:
            result += fetch_node_details(parent_id, "Parent Node")
        else:
            result += "This is a root node (No parent).\n\n"
            
        # Fetch Children
        if children_ids:
            result += f"#### Children Nodes ({len(children_ids)} total):\n"
            for child_id in children_ids:
                result += fetch_node_details(child_id, "Child Node")
        else:
            result += "This node has no children.\n"
        return result
        
    except Exception as e:
        return f"Database retrieval error: {str(e)}"

async def find_interface_impacts_impl(interface_id: str) -> str:
    """
    Find all interfaces that call the given interface_id (static analysis via traceability DB).
    """
    try:
        rows = [
            iface
            for iface in _store().list_interfaces()
            if interface_id in (iface.get("callees") or [])
        ]
        
        if not rows:
            return f"No interfaces found that call '{interface_id}'. It is safe to modify."
            
        result = f"### Impact Analysis for Interface [{interface_id}]\n"
        result += "The following interfaces call this interface and might be affected by your changes:\n\n"
        
        for row in rows:
            result += f"- **ID**: {row['interface_id']} (Type: {row['type']})\n"
            if row.get("file_path"):
                result += f"  - Path: `{row['file_path']}`\n"
            if row.get("first_line"):
                result += f"  - Signature: `{row['first_line']}`\n"
            result += f"  - Used in Req IDs: {row.get('req_ids', [])}\n\n"
            
        return result
        
    except Exception as e:
        return f"Database retrieval error: {str(e)}"

async def search_interfaces_by_keyword_impl(keyword: str, limit: int = 10) -> str:
    """
    Search for interfaces by keyword in their name or description.
    Useful for finding reusable functionality like 'auth', 'database', 'user'.
    """
    try:
        normalized_keyword = str(keyword or "").strip().lower()
        rows = []
        for iface in _store().list_interfaces():
            haystacks = [
                str(iface.get("interface_id", "")).lower(),
                str(iface.get("content", "")).lower(),
            ]
            if any(normalized_keyword in haystack for haystack in haystacks):
                rows.append(iface)
            if len(rows) >= limit:
                break
        
        if not rows:
            return f"No interfaces found matching keyword: '{keyword}'"
            
        result = f"### Interfaces matching '{keyword}'\n\n"
        for row in rows:
            result += f"- **ID**: `{row['interface_id']}` (Type: {row['type']})\n"
            if row.get("file_path"):
                result += f"  - Path: `{row['file_path']}`\n"
            if row.get("first_line"):
                result += f"  - Signature: `{row['first_line']}`\n"
            try:
                content = json.loads(row.get("content", ""))
                if 'name' in content and content['name']:
                    result += f"  - Name: {content['name']}\n"
                if 'description' in content and content['description']:
                    result += f"  - Description: {content['description']}\n"
            except:
                pass
            result += f"  - Used in Req IDs: {row.get('req_ids', [])}\n\n"
            
        return result
        
    except Exception as e:
        return f"Database search error: {str(e)}"

async def search_interfaces_by_relation_impl(node_id: str, relation_type: str = "all") -> str:
    """
    Find interfaces belonging to related requirement nodes (parent, children, siblings, dependencies).
    relation_type can be: 'parent', 'children', 'siblings', 'dependencies', 'all'
    """
    try:
        store = _store()
        node_row = store.get_requirement(node_id)
        if not node_row:
            return f"Requirement node '{node_id}' not found."
            
        parent_id = node_row.get("parent_id")
        children_ids = node_row.get("children_ids") or []
        dependencies = node_row.get("dependencies") or []
            
        siblings = []
        if parent_id:
            parent_req = store.get_requirement(parent_id)
            if parent_req:
                siblings = [c for c in (parent_req.get("children_ids") or []) if c != node_id]

        target_nodes = set()
        if relation_type in ["parent", "all"] and parent_id:
            target_nodes.add(parent_id)
        if relation_type in ["children", "all"]:
            target_nodes.update(children_ids)
        if relation_type in ["siblings", "all"]:
            target_nodes.update(siblings)
        if relation_type in ["dependencies", "all"]:
            target_nodes.update(dependencies)
            
        if not target_nodes:
            return f"No related nodes found for relation type: {relation_type}"

        # 2. Fetch interfaces for these nodes
        result = f"### Interfaces in Related Nodes (Relation: {relation_type})\n\n"
        found_any = False
        
        for n_id in target_nodes:
            ifaces = store.list_interfaces(req_id=n_id)
            
            if ifaces:
                found_any = True
                result += f"#### From Node [{n_id}]:\n"
                for row in ifaces:
                    result += f"- **ID**: `{row['interface_id']}` (Type: {row['type']})\n"
                    if row.get("file_path"):
                        result += f"  - Path: `{row['file_path']}`\n"
                    if row.get("first_line"):
                        result += f"  - Signature: `{row['first_line']}`\n"
                    try:
                        content = json.loads(row.get("content", ""))
                        if 'description' in content and content['description']:
                            result += f"  - Description: {content['description']}\n"
                    except:
                        pass
                result += "\n"
        
        if not found_any:
            return f"Related nodes found, but they do not have any designed interfaces yet."
            
        return result
        
    except Exception as e:
        return f"Database relation search error: {str(e)}"
