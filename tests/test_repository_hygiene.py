from pathlib import Path
import subprocess


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


def test_private_runtime_paths_are_not_tracked() -> None:
    private_paths = {
        ".env",
        ".venv-ragas",
        "artifacts",
        "chroma_db",
        "memory_chroma_db",
        "resumes",
        "鐩稿叧鏉愭枡",
    }

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    tracked_top_level_paths = {
        Path(path.decode("utf-8")).parts[0]
        for path in result.stdout.split(b"\0")
        if path
    }

    assert not (private_paths & tracked_top_level_paths)
