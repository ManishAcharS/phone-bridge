"""Release-metadata checks: the things that only break at ship time.

None of these exercise the protocol. They exist because the failures they catch
are invisible until someone tries to install the package or publish the app:

* a version string that drifted between the three places it appears;
* a `pyproject.toml` that no longer builds (PEP 639 will happily make
  `pip install` refuse the project);
* a signing keystore that is not ignored by git;
* a command that exists but is in no document, or a documentation link that
  points at a file that was renamed.

Every assertion reads the real files. Nothing is hard-coded to "the way it is
today" except the invariants themselves.
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

REPO = Path(__file__).resolve().parents[2]
WINDOWS = REPO / "windows"
DOCS = REPO / "docs"
README = REPO / "README.md"

PYPROJECT = WINDOWS / "pyproject.toml"
INIT = WINDOWS / "phonebridge" / "__init__.py"
GRADLE = REPO / "android" / "app" / "build.gradle.kts"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def project() -> dict:
    return tomllib.loads(read(PYPROJECT))["project"]


# --------------------------------------------------------------------------- #
# 1. one version, everywhere
# --------------------------------------------------------------------------- #
def test_the_version_is_the_same_in_packaging_and_in_the_app() -> None:
    pkg_version = project()["version"]
    module_version = re.search(r'^__version__ = "([^"]+)"', read(INIT), re.M)
    app_version = re.search(r'versionName = "([^"]+)"', read(GRADLE))
    assert module_version, "__version__ is missing from phonebridge/__init__.py"
    assert app_version, "versionName is missing from android/app/build.gradle.kts"
    assert pkg_version == module_version.group(1) == app_version.group(1), (
        "the release version must be identical in pyproject.toml, "
        f"__init__.py and build.gradle.kts (got {pkg_version!r}, "
        f"{module_version.group(1)!r}, {app_version.group(1)!r})"
    )


def test_the_version_is_a_plain_release_string() -> None:
    version = project()["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
        f"{version!r} is not a clean X.Y.Z release version; PEP 440 pre-release "
        "suffixes are fine for PyPI but make the Android versionName ambiguous"
    )


def test_android_version_code_is_a_positive_integer() -> None:
    match = re.search(r"versionCode = (\d+)", read(GRADLE))
    assert match, "versionCode is missing"
    assert int(match.group(1)) >= 1, "versionCode must be >= 1"


# --------------------------------------------------------------------------- #
# 2. package identity
# --------------------------------------------------------------------------- #
def test_android_package_identity_is_stable() -> None:
    text = read(GRADLE)
    namespace = re.search(r'namespace = "([^"]+)"', text)
    app_id = re.search(r'applicationId = "([^"]+)"', text)
    assert namespace and app_id
    assert namespace.group(1) == app_id.group(1) == "dev.phonebridge", (
        "namespace and applicationId must match: the applicationId is the "
        "identity Android uses to decide whether an install is an update"
    )


def test_sdk_levels_are_coherent() -> None:
    text = read(GRADLE)
    levels = {
        key: int(re.search(rf"{key} = (\d+)", text).group(1))
        for key in ("minSdk", "compileSdk", "targetSdk")
    }
    assert levels["minSdk"] >= 26, "the app relies on APIs that require API 26+"
    assert levels["minSdk"] <= levels["targetSdk"] <= levels["compileSdk"], levels


# --------------------------------------------------------------------------- #
# 3. the metadata the package index reads
# --------------------------------------------------------------------------- #
def test_pyproject_has_the_metadata_a_release_needs() -> None:
    meta = project()
    for key in ("name", "version", "description", "readme", "license", "requires-python"):
        assert meta.get(key), f"{key} is missing from [project]"
    assert meta["name"] == "phonebridge"
    assert meta["license"] == "MIT", "PEP 639 license expression"
    assert meta["urls"], "[project.urls] must point at the source repository"
    assert any("Homepage" in key for key in meta["urls"])
    assert meta["classifiers"], "classifiers are what a package index displays"


def test_the_license_expression_is_not_duplicated_by_a_classifier() -> None:
    """setuptools >= 77 refuses to build when both are present.

    This is not style: the combination produced `InvalidConfigError` and made
    `pip install .` fail completely (found by installing into a clean venv).
    """
    meta = project()
    if meta.get("license") and isinstance(meta["license"], str):
        offenders = [c for c in meta["classifiers"] if c.startswith("License ::")]
        assert not offenders, (
            f"license classifiers are not allowed with an SPDX license string: {offenders}"
        )


def test_build_requires_a_setuptools_that_understands_the_metadata() -> None:
    requires = tomllib.loads(read(PYPROJECT))["build-system"]["requires"]
    pins = [r for r in requires if r.startswith("setuptools")]
    assert pins, requires
    version = re.search(r">=\s*(\d+)", pins[0])
    assert version and int(version.group(1)) >= 77, (
        "the SPDX `license = \"MIT\"` field needs setuptools >= 77; older "
        "releases want a license table and print a removal warning"
    )


def test_the_console_script_points_at_a_real_callable() -> None:
    from phonebridge.cli import main as cli_main

    target = project()["scripts"]["phonebridge"]
    module_name, func_name = target.split(":")
    assert module_name == "phonebridge.cli.main"
    assert callable(getattr(cli_main, func_name, None)), f"{target} is not callable"


# --------------------------------------------------------------------------- #
# 4. signing material must never be committed
# --------------------------------------------------------------------------- #
def test_signing_material_is_git_ignored() -> None:
    ignore = read(REPO / ".gitignore")
    for pattern in ("android/keystore.properties", "*.jks", "*.keystore"):
        assert pattern in ignore, f"{pattern} must be in .gitignore"
    assert "!android/keystore.properties" not in ignore, "a negation would undo the ignore"


# --------------------------------------------------------------------------- #
# 5. the documentation set is internally consistent
# --------------------------------------------------------------------------- #
def test_every_cli_command_appears_in_the_command_reference() -> None:
    from phonebridge.cli.main import build_parser

    reference = read(DOCS / "11-pc-client.md")
    parser = build_parser()
    subparsers = [
        action for action in parser._actions if action.__class__.__name__ == "_SubParsersAction"
    ]
    assert subparsers, "the CLI has no subcommands?"
    commands = sorted(subparsers[0].choices)
    missing = [name for name in commands if f"`{name}" not in reference and f" {name} " not in reference]
    assert not missing, f"commands missing from docs/11-pc-client.md: {missing}"


def test_relative_markdown_links_resolve() -> None:
    """A link to a renamed document is a dead end for a new user."""
    broken: list[str] = []
    for path in [README, *sorted(DOCS.glob("*.md"))]:
        base = path.parent
        for target in re.findall(r"\]\(([^)#][^)]*)\)", read(path)):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            candidate = (base / target.split("#")[0]).resolve()
            if not candidate.exists():
                broken.append(f"{path.relative_to(REPO)} -> {target}")
    assert not broken, "broken relative links: " + ", ".join(broken)


def _documented_cli_lines() -> list[tuple[str, str]]:
    """Every ``phonebridge ...`` line inside a fenced block, with its file.

    Backtick line continuations are joined, trailing ``# comments`` are dropped,
    prompt characters are stripped, and PowerShell's own verbs (``pip``,
    ``adb``, ``.\\gradlew.bat``) are ignored: only lines that invoke *this* CLI
    are returned.
    """
    lines: list[tuple[str, str]] = []
    for path in [README, *sorted(DOCS.glob("*.md"))]:
        text = read(path)
        for block in re.findall(r"```[a-zA-Z0-9]*\n(.*?)```", text, re.S):
            joined = re.sub(r"`\s*\n\s*", " ", block)
            for raw in joined.splitlines():
                # ``# comment``, ``-> expected output`` and ``| Next-Command`` are
                # annotations around the invocation, not part of it.
                line = re.split(r"\s+#\s", raw, maxsplit=1)[0]
                line = re.split(r"\s*->\s*", line, maxsplit=1)[0]
                line = re.split(r"\s*\|\s*", line, maxsplit=1)[0]
                line = line.strip().lstrip("PS>").lstrip(">").strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("python -m phonebridge "):
                    line = "phonebridge " + line[len("python -m phonebridge ") :]
                if not line.startswith("phonebridge "):
                    continue
                lines.append((str(path.relative_to(REPO)), line))
    assert lines, "no documented CLI invocations were found - has the docs format changed?"
    return lines


def _subparsers(parser):
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return action
    return None


def _flags(parser) -> dict[str, bool]:
    """option string -> whether it consumes a value."""
    takes_value = {}
    for action in parser._actions:
        consumes = action.nargs != 0 and action.__class__.__name__ not in {
            "_StoreTrueAction",
            "_StoreFalseAction",
            "_HelpAction",
            "_VersionAction",
            "_CountAction",
        }
        for option in action.option_strings:
            takes_value[option] = consumes
    return takes_value


def test_every_documented_cli_invocation_uses_real_commands_and_flags() -> None:
    """Documentation drift is a release bug: a flag that does not exist is a
    command that does not run.

    This caught a real typo in a draft of the new README (``--no-discovery``
    instead of ``--no-discover``), which nothing else here could have noticed,
    because nothing else reads the prose.
    """
    from phonebridge.cli.main import build_parser

    root = build_parser()
    problems: list[str] = []
    for where, line in _documented_cli_lines():
        tokens = line.split()
        parser = root
        known = _flags(root)
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token.startswith("-"):
                flag = token.split("=")[0]
                if flag in {"-h", "--help"}:
                    index += 1
                    continue
                if flag in known:
                    index += 2 if known[flag] and "=" not in token else 1
                    continue
                problems.append(f"{where}: {line.strip()!r} uses unknown flag {flag!r}")
                # assume it takes a value so one bad flag does not cascade
                index += 2 if "=" not in token and not token.startswith("--no-") else 1
                continue
            nested = _subparsers(parser)
            if nested is None:
                index += 1  # a positional argument (host, phone id, ...)
                continue
            if token not in nested.choices:
                problems.append(f"{where}: {line.strip()!r} uses unknown command {token!r}")
                break
            parser = nested.choices[token]
            known = {**known, **_flags(parser)}
            index += 1
    assert not problems, "documented invocations that would fail:\n  " + "\n  ".join(problems)


def test_the_release_documents_exist_and_are_linked() -> None:
    readme = read(README)
    for name in (
        "14-release-readiness.md",
        "15-compatibility-report.md",
        "16-known-limitations.md",
    ):
        assert (DOCS / name).exists(), f"docs/{name} is missing"
        assert name in readme, f"docs/{name} is not linked from the README"
