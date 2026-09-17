"""List the static worker-only source closure; never package X/cloud authority."""

import ast
from pathlib import Path


def worker_modules(root: Path) -> tuple[str, ...]:
    pending = ["captain_worker_cli", "__init__"]
    selected = set()
    while pending:
        module = pending.pop()
        if module in selected:
            continue
        if module.startswith("x_") or module in {"publisher", "test_post", "app"}:
            raise ValueError("posting dependency reachable from worker")
        tree = ast.parse((root / f"{module}.py").read_text(encoding="utf-8"))
        selected.add(module)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("fpl_bot."):
                    pending.append(node.module.removeprefix("fpl_bot."))
            elif isinstance(node, ast.Import):
                pending.extend(
                    item.name.removeprefix("fpl_bot.")
                    for item in node.names
                    if item.name.startswith("fpl_bot.")
                )
    return tuple(sorted(selected))


if __name__ == "__main__":
    for name in worker_modules(Path("src/fpl_bot")):
        print(name)
