"""
ast_tool.py — Python AST parser for CodeLineage knowledge graph

Responsibility:
    Takes a Python source file as a string
    Returns structured JSON describing:
        - every function + class method (name, args, return type, calls, called_by, decorators)
        - every class                   (methods, base classes, decorators)
        - every DB model                (SQLAlchemy/Django/Pydantic classes + fields)
        - every API route               (FastAPI/Flask decorators)
        - imports                       (what this file pulls in from other files)
        - exports                       (which functions/classes are used by other files)
        - dependents                    (which files depend on this file)

Called by:
    github_tool.py — for every .py file fetched from GitHub
"""

import ast
import json


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CORE ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def parse_file(source_code: str, file_path: str) -> dict:
    """
    Parses one .py file into structured knowledge graph data.

    Args:
        source_code : raw content of a .py file as a string
        file_path   : path in the repo e.g. "services/user.py"

    Returns:
        {
            file_path:  "services/user.py",
            functions:  { name → function info },
            classes:    { name → class info },
            db_models:  { name → model info },
            api_routes: { "GET /path" → route info },
            imports:    { "services.user" → ["get_user"] },
            exports:    { "get_user" → [] },
            dependents: []
        }
    """
    result = {
        "file_path":  file_path,
        "functions":  {},       # top-level functions only
        "classes":    {},       # all classes with their methods
        "db_models":  {},       # classes that are DB models
        "api_routes": {},       # FastAPI/Flask route decorators
        "imports":    {},       # local project imports only
        "exports":    {},       # functions/classes used by other files
        "dependents": [],       # files that import this file
    }

    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        print(f"[SKIP] Syntax error in {file_path}: {e}")
        return result

    # walk only the TOP LEVEL of the tree
    # so we get top-level functions and classes without going too deep
    for node in ast.iter_child_nodes(tree):

        # ── Top-level functions ────────────────────────────────────────────────
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result["functions"][node.name] = _parse_function(node, file_path)

        # ── Classes — regular + DB models ─────────────────────────────────────
        elif isinstance(node, ast.ClassDef):
            result["classes"][node.name] = _parse_class(node, file_path)

            if _is_db_model(node):
                result["db_models"][node.name] = _parse_db_model(node, file_path)

    # ── API routes — needs full tree scan for decorators ──────────────────────
    result["api_routes"] = _parse_api_routes(tree, file_path)

    # ── Imports — local project only ──────────────────────────────────────────
    result["imports"] = _parse_imports(tree)

    # every top-level function and class starts as a potential export
    # link_exports() will fill in which files actually import each one
    result["exports"] = {
        name: []
        for name in list(result["functions"].keys()) + list(result["classes"].keys())
    }

    return result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — FUNCTION PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _parse_function(node: ast.FunctionDef, file_path: str) -> dict:
    """
    Extracts everything we need from one function definition.

    Input (AST node for):
        @login_required
        @cache(timeout=60)
        def get_user(user_id: int, db: Session) -> User:
            result = db.query(User).filter(User.id == user_id).first()
            return serialize_user(result)

    Output:
        {
            name:        "get_user",
            file:        "services/user.py",
            line_number: 4,
            args:        ["user_id", "db"],
            returns:     "User",
            calls:       ["db.query", "serialize_user"],
            called_by:   [],
            decorators:  ["login_required", "cache"]
        }
    """
    return {
        "name":        node.name,
        "file":        file_path,
        "line_number": node.lineno,
        "args":        _get_args(node),
        "returns":     _get_return_type(node),
        "calls":       _get_calls(node),
        "called_by":   [],
        "decorators":  _get_decorators(node),
    }


def _get_args(node: ast.FunctionDef) -> list[str]:
    """
    Extracts argument names, skipping self and cls.

    def get_user(self, user_id, db) → ["user_id", "db"]
    """
    return [
        arg.arg
        for arg in node.args.args
        if arg.arg not in ("self", "cls")
    ]


def _get_return_type(node: ast.FunctionDef) -> str:
    """
    Reads the return type annotation if present.

    def get_user(...) -> User  →  "User"
    def get_user(...)          →  "unknown"
    """
    if node.returns is None:
        return "unknown"
    return ast.unparse(node.returns)


def _get_calls(node: ast.FunctionDef) -> list[str]:
    """
    Walks a function body and collects every function it calls.

    def delete_user(user_id, db):
        user = get_user(user_id, db)   → "get_user"
        db.delete(user)                → "db.delete"
        db.commit()                    → "db.commit"

    Two call patterns:
        simple call    → get_user(...)   → "get_user"
        attribute call → db.query(...)   → "db.query"

    This is the KEY relationship — when get_user() changes
    we find every function whose calls list includes "get_user".
    """
    calls = []
    seen  = set()

    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue

        if isinstance(child.func, ast.Name):
            name = child.func.id

        elif isinstance(child.func, ast.Attribute):
            obj  = ast.unparse(child.func.value)
            name = f"{obj}.{child.func.attr}"

        else:
            continue

        if name not in seen:
            seen.add(name)
            calls.append(name)

    return calls


def _get_decorators(node: ast.FunctionDef) -> list[str]:
    """
    Extracts decorator names from a function or method.

    @login_required
    @cache(timeout=60)
    @permission_classes([IsAuthenticated])
    def get_user():
        ...

    Output: ["login_required", "cache", "permission_classes"]

    Why this matters:
        - @login_required means callers need to be authenticated
        - @permission_classes controls who can call this route
        - @cache means changing this function may serve stale data
        - @receiver means this is a Django signal handler

    If a PR removes @login_required from a function that had it,
    that's a security-relevant change the impact analyzer must flag.
    """
    decorators = []

    for decorator in node.decorator_list:
        # plain decorator: @login_required
        if isinstance(decorator, ast.Name):
            decorators.append(decorator.id)

        # decorator with args: @cache(timeout=60)
        elif isinstance(decorator, ast.Call):
            if isinstance(decorator.func, ast.Name):
                decorators.append(decorator.func.id)
            elif isinstance(decorator.func, ast.Attribute):
                decorators.append(decorator.func.attr)

        # attribute decorator: @app.route(...)
        elif isinstance(decorator, ast.Attribute):
            decorators.append(decorator.attr)

    return decorators


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — CLASS PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _parse_class(node: ast.ClassDef, file_path: str) -> dict:
    """
    Extracts everything we need from a class definition.

    Input:
        class UserService(BaseService):
            def get_user(self, user_id: int) -> User:
                ...
            def create_user(self, name: str) -> User:
                ...

    Output:
        {
            name:        "UserService",
            file:        "services/user.py",
            line_number: 1,
            bases:       ["BaseService"],
            decorators:  [],
            methods:     {
                "get_user": {
                    name, file, line_number, args,
                    returns, calls, called_by, decorators
                },
                "create_user": { ... }
            }
        }

    Why class methods matter:
        In Django and most Python projects, business logic lives in
        classes — services, repositories, ViewSets, APIViews.
        Without parsing class methods we'd miss most of the codebase.
    """
    # extract base class names e.g. ["BaseService", "APIView"]
    bases = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute):
            bases.append(f"{ast.unparse(base.value)}.{base.attr}")

    # extract class-level decorators e.g. @dataclass, @admin.register(User)
    decorators = _get_decorators(node)

    # extract all methods defined directly in this class
    methods = {}
    for item in ast.iter_child_nodes(node):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods[item.name] = _parse_function(item, file_path)

    return {
        "name":        node.name,
        "file":        file_path,
        "line_number": node.lineno,
        "bases":       bases,
        "decorators":  decorators,
        "methods":     methods,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — DB MODEL PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _is_db_model(node: ast.ClassDef) -> bool:
    """
    Checks if a class is a DB model by its base class.

    class User(Base):          → SQLAlchemy  ✓
    class User(BaseModel):     → Pydantic    ✓
    class User(models.Model):  → Django      ✓
    class User(SomeHelper):    → other       ✗
    """
    model_bases = {"Base", "BaseModel", "Model"}

    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in model_bases:
            return True
        if isinstance(base, ast.Attribute) and base.attr in model_bases:
            return True

    return False


def _parse_db_model(node: ast.ClassDef, file_path: str) -> dict:
    """
    Extracts field names from a DB model class.

    class User(Base):
        id    = Column(Integer, primary_key=True)
        name  = Column(String)
        email = Column(String)

    Output:
        {
            name:        "User",
            file:        "models/user.py",
            line_number: 2,
            fields:      ["id", "name", "email"]
        }

    If a PR removes the "email" field → every function that
    accesses user.email is potentially broken.
    """
    fields = []

    for item in node.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    fields.append(target.id)

        elif isinstance(item, ast.AnnAssign):
            if isinstance(item.target, ast.Name):
                fields.append(item.target.id)

    return {
        "name":        node.name,
        "file":        file_path,
        "line_number": node.lineno,
        "fields":      fields,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — API ROUTE PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _parse_api_routes(tree: ast.Module, file_path: str) -> dict:
    """
    Finds FastAPI/Flask route decorators and extracts route info.

    @router.get("/users/{user_id}")
    async def user_detail(user_id: int):
        ...

    Output:
        {
            "GET /users/{user_id}": {
                path:        "/users/{user_id}",
                method:      "GET",
                handler:     "user_detail",
                file:        "routes/user.py",
                line_number: 4
            }
        }

    If the handler's signature changes → the API contract may break.
    """
    routes       = {}
    http_methods = {"get", "post", "put", "patch", "delete"}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if not isinstance(decorator.func, ast.Attribute):
                continue

            method = decorator.func.attr.lower()
            if method not in http_methods:
                continue

            if not decorator.args:
                continue

            path_node = decorator.args[0]
            if not isinstance(path_node, ast.Constant):
                continue

            path = path_node.value
            key  = f"{method.upper()} {path}"

            routes[key] = {
                "path":        path,
                "method":      method.upper(),
                "handler":     node.name,
                "file":        file_path,
                "line_number": node.lineno,
            }

    return routes


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — IMPORT PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _parse_imports(tree: ast.Module) -> dict:
    """
    Extracts all local project imports from a file.

    from services.user import get_user, create_user  → tracked
    from models.user import User                     → tracked
    import os                                        → skipped (stdlib)
    import fastapi                                   → skipped (third party)

    Output:
        {
            "services.user": ["get_user", "create_user"],
            "models.user":   ["User"]
        }

    We only track local project imports because we only care about
    relationships within the codebase — not third party dependencies.
    """
    imports = {}

    skip_prefixes = {
        "os", "sys", "json", "re", "math", "time", "datetime",
        "typing", "collections", "itertools", "functools", "pathlib",
        "fastapi", "sqlalchemy", "pydantic", "starlette", "uvicorn",
        "langchain", "langgraph", "github", "dotenv", "asyncio",
        "logging", "hashlib", "hmac", "abc", "dataclasses", "enum",
        "django", "rest_framework", "celery", "redis", "boto3",
    }

    for node in ast.walk(tree):

        # from services.user import get_user, create_user
        if isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            root = node.module.split(".")[0]
            if root in skip_prefixes:
                continue
            names = [alias.name for alias in node.names]
            if node.module not in imports:
                imports[node.module] = []
            imports[node.module].extend(names)

        # import services.user
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in skip_prefixes:
                    continue
                if alias.name not in imports:
                    imports[alias.name] = []

    return imports


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — LINK RELATIONSHIPS ACROSS ALL FILES
# ══════════════════════════════════════════════════════════════════════════════

def link_called_by(knowledge_graph: dict) -> dict:
    """
    Fills in called_by for every function and method across all files.

    If get_user.calls includes "serialize_user"
    → serialize_user.called_by gets "get_user" added

    Also handles class methods — if UserService.get_user calls
    serialize_user, that relationship is captured too.

    Answers: "who calls this function?"
    Used by impact analyzer to find all affected callers when
    a function changes.
    """
    # flat map of ALL functions — top level + class methods
    all_functions = {}

    for file_data in knowledge_graph.values():

        # top-level functions
        for fname, finfo in file_data["functions"].items():
            all_functions[fname] = finfo

        # class methods — stored as ClassName.method_name
        for class_name, class_info in file_data["classes"].items():
            for method_name, method_info in class_info["methods"].items():
                key = f"{class_name}.{method_name}"
                all_functions[key] = method_info

                # also index by method name alone for simple call matching
                # e.g. delete_user calls "get_user" not "UserService.get_user"
                if method_name not in all_functions:
                    all_functions[method_name] = method_info

    # fill in called_by
    for caller_name, caller_info in all_functions.items():
        for called_name in caller_info["calls"]:
            if called_name in all_functions:
                if caller_name not in all_functions[called_name]["called_by"]:
                    all_functions[called_name]["called_by"].append(caller_name)

    return knowledge_graph


def link_exports(knowledge_graph: dict) -> dict:
    """
    Fills in exports and dependents across all files.

    If routes/user_routes.py imports get_user from services.user:
        → services/user.py  exports["get_user"] += "routes/user_routes.py"
        → services/user.py  dependents          += "routes/user_routes.py"

    Answers two questions:

    1. exports — "which functions/classes in this file are used elsewhere?"
       Used to know what's safe to change vs what will break other files.

    2. dependents — "which files depend on this file?"
       Used for incremental indexing — when this file changes,
       only re-parse its dependents, not the entire repo.
    """
    # map "services.user" → "services/user.py"
    module_to_file = {
        file_path.replace("/", ".").replace(".py", ""): file_path
        for file_path in knowledge_graph
    }

    for consumer_path, consumer_data in knowledge_graph.items():
        for module_name, imported_names in consumer_data["imports"].items():

            provider_path = module_to_file.get(module_name)
            if provider_path is None:
                continue

            provider_data = knowledge_graph[provider_path]

            # mark consumer as a dependent of provider
            if consumer_path not in provider_data["dependents"]:
                provider_data["dependents"].append(consumer_path)

            # mark each imported name as exported to this consumer
            for fname in imported_names:
                if fname in provider_data["exports"]:
                    if consumer_path not in provider_data["exports"][fname]:
                        provider_data["exports"][fname].append(consumer_path)

    return knowledge_graph


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def build_knowledge_graph(files: list[dict]) -> dict:
    """
    Full pipeline — parses all files and links all relationships.

    Args:
        files: list of {"path": "services/user.py", "content": "..."}
               this is exactly what github_tool.py will provide

    Returns:
        complete knowledge graph with all relationships linked

    Steps:
        1. parse every file individually   (sections 1-6)
        2. link called_by                  (who calls who)
        3. link exports + dependents       (cross-file imports)
    """
    knowledge_graph = {}

    for f in files:
        parsed = parse_file(f["content"], f["path"])
        knowledge_graph[f["path"]] = parsed

    knowledge_graph = link_called_by(knowledge_graph)
    knowledge_graph = link_exports(knowledge_graph)

    return knowledge_graph