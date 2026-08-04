from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_repository_files_exist() -> None:
    required_files = (
        "README.md",
        ".gitignore",
        ".env.example",
        "LICENSE",
        "pyproject.toml",
    )

    assert all((ROOT / name).is_file() for name in required_files)


def test_private_runtime_directories_are_not_in_repository() -> None:
    private_directories = {
        ".env",
        ".venv-ragas",
        "artifacts",
        "chroma_db",
        "memory_chroma_db",
        "resumes",
        "鐩稿叧鏉愭枡",
    }

    assert not (private_directories & {entry.name for entry in ROOT.iterdir()})
