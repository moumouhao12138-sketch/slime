from __future__ import annotations

from pathlib import Path


class IsolatedWorkspace:
    """A project-local filesystem boundary for notes, scripts and evidence."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("notes", "scripts", "evidence/actions", "reports"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(f"路径越出伪足工作区: {relative_path}") from exc
        return candidate

    def write_text(self, relative_path: str, content: str) -> str:
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path.relative_to(self.root).as_posix()

    def read_text(self, relative_path: str, max_chars: int = 20_000) -> str:
        return self.resolve(relative_path).read_text(encoding="utf-8")[:max_chars]

    def list_files(self, relative_path: str = ".") -> list[str]:
        base = self.resolve(relative_path)
        if not base.exists():
            return []
        if base.is_file():
            return [base.relative_to(self.root).as_posix()]
        return sorted(path.relative_to(self.root).as_posix() for path in base.rglob("*") if path.is_file())
