"""Static checks on the Android build configuration.

These exist because the Android app cannot be compiled in this environment (no
JDK, no SDK). Everything here is a property that *can* be proven by reading the
files, and each one is a mistake that would otherwise only surface after a
failed build or on a device:

* a missing or wrong Gradle wrapper (the repo must build from a fresh clone)
* an AGP / Gradle / Kotlin / Compose / compileSdk combination that is rejected
* a foreground-service permission prerequisite missing at runtime
* a dependency coordinate or dynamic version that breaks reproducibility
* a build file pointing at a file that does not exist

None of this is a substitute for `./gradlew assembleDebug`; it only guarantees
the build cannot fail *for these reasons* before you spend time on it.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ANDROID = REPO / "android"
WRAPPER = ANDROID / "gradle" / "wrapper"
APP_BUILD = ANDROID / "app" / "build.gradle.kts"
ROOT_BUILD = ANDROID / "build.gradle.kts"
PROPS = ANDROID / "gradle.properties"
WRAPPER_PROPS = WRAPPER / "gradle-wrapper.properties"
MANIFEST = ANDROID / "app" / "src" / "main" / "AndroidManifest.xml"
KOTLIN_DIR = ANDROID / "app" / "src" / "main" / "java" / "dev" / "phonebridge"

# --------------------------------------------------------------------------- #
# known-compatible pairings (see docs/06-phase1-audit.md §4)
# --------------------------------------------------------------------------- #
#: Minimum Gradle for each AGP line, from the AGP release notes.
AGP_MIN_GRADLE = {
    "8.3": (8, 4),
    "8.4": (8, 6),
    "8.5": (8, 7),
    "8.6": (8, 7),
    "8.7": (8, 9),
}

#: Compose compiler extension for each Kotlin line.
KOTLIN_COMPOSE_COMPILER = {
    "1.9.22": "1.5.8",
    "1.9.23": "1.5.11",
    "1.9.24": "1.5.14",
    "2.0.0": "1.5.15",
}

#: Highest compileSdk each AGP line supports.
AGP_MAX_COMPILE_SDK = {"8.3": 34, "8.4": 34, "8.5": 34, "8.6": 35, "8.7": 35}

#: Compose BOM lines that require compileSdk 35 and AGP 8.6+.
BOM_REQUIRES_SDK_35_FROM = "2024.09"

#: Permissions that satisfy the `connectedDevice` precondition for a
#: foreground service on Android 14+ (any one of them).
FGS_CONNECTED_DEVICE_PREREQUISITES = {
    "android.permission.BLUETOOTH_ADVERTISE",
    "android.permission.BLUETOOTH_CONNECT",
    "android.permission.BLUETOOTH_SCAN",
    "android.permission.CHANGE_NETWORK_STATE",
    "android.permission.CHANGE_WIFI_STATE",
    "android.permission.CHANGE_WIFI_MULTICAST_STATE",
    "android.permission.NFC",
    "android.permission.TRANSMIT_IR",
    "android.permission.UWB_RANGING",
    "android.permission.USB_PERMISSION",
}


def read(path: Path) -> str:
    return path.read_text("utf-8")


def gradle_version() -> str:
    match = re.search(r"distributionUrl=.*gradle-([\d.]+)-(bin|all)\.zip", read(WRAPPER_PROPS))
    assert match, "distributionUrl does not look like a Gradle distribution"
    return match.group(1)


def agp_version() -> str:
    match = re.search(r'id\("com\.android\.application"\)\s+version\s+"([\d.]+)"', read(ROOT_BUILD))
    assert match, "AGP version not found in build.gradle.kts"
    return match.group(1)


def kotlin_version() -> str:
    match = re.search(r'id\("org\.jetbrains\.kotlin\.android"\)\s+version\s+"([\d.]+)"', read(ROOT_BUILD))
    assert match, "Kotlin version not found in build.gradle.kts"
    return match.group(1)


def app_config() -> dict[str, str]:
    text = read(APP_BUILD)
    out = {}
    for key in ("compileSdk", "minSdk", "targetSdk", "versionCode", "kotlinCompilerExtensionVersion"):
        match = re.search(rf"\b{key}\s*=\s*\"?([\w.\-]+)\"?", text)
        if match:
            out[key] = match.group(1).strip('"')
    return out


def permissions() -> set[str]:
    # Tolerate the multi-line form used for permissions that carry attributes
    # such as android:maxSdkVersion.
    return set(re.findall(r'<uses-permission\s+android:name="([^"]+)"', read(MANIFEST)))


# --------------------------------------------------------------------------- #
# 1. the wrapper must be present and usable from a fresh clone
# --------------------------------------------------------------------------- #
def test_wrapper_jar_is_present_and_is_the_gradle_wrapper():
    jar = WRAPPER / "gradle-wrapper.jar"
    assert jar.is_file(), (
        f"{jar} is missing. Without it `gradlew` cannot start on a machine that "
        "has no system Gradle installed."
    )
    with zipfile.ZipFile(jar) as zf:
        assert zf.testzip() is None, "wrapper jar is corrupt"
        names = set(zf.namelist())
    assert "org/gradle/wrapper/GradleWrapperMain.class" in names, (
        "jar has no GradleWrapperMain - it is not a Gradle wrapper jar"
    )


@pytest.mark.parametrize("script", ["gradlew", "gradlew.bat"])
def test_wrapper_scripts_exist_and_launch_the_wrapper_main(script: str):
    path = ANDROID / script
    assert path.is_file(), f"{path} is missing"
    text = read(path)
    assert "org.gradle.wrapper.GradleWrapperMain" in text
    assert "gradle-wrapper.jar" in text
    assert len(text) > 1000, "suspiciously small wrapper script"


def test_gradlew_is_executable_in_git():
    """A non-executable gradlew is a confusing `Permission denied` on Linux/macOS."""
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "-s", "android/gradlew"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("not a git checkout or file not staged yet")
    mode = result.stdout.split()[0]
    assert mode == "100755", f"gradlew mode is {mode}, expected 100755"


def test_gitattributes_pins_line_endings():
    text = read(REPO / ".gitattributes")
    assert re.search(r"^\*\.bat\s+text\s+eol=crlf", text, re.M), (
        "gradlew.bat needs CRLF on checkout or cmd.exe can mis-parse it"
    )
    assert re.search(r"^gradlew\s+text\s+eol=lf", text, re.M), (
        "gradlew needs LF or /bin/sh reports a bad interpreter"
    )


def test_wrapper_jar_is_not_gitignored():
    gitignore = read(REPO / ".gitignore")
    assert "!android/gradle/wrapper/gradle-wrapper.jar" in gitignore or "*.jar" not in gitignore


def test_gradle_version_satisfies_agp_requirement():
    gradle = gradle_version()
    agp = agp_version()
    line = ".".join(agp.split(".")[:2])
    required = AGP_MIN_GRADLE.get(line)
    assert required, f"unknown AGP line {line}; update AGP_MIN_GRADLE"
    gradle_tuple = tuple(int(p) for p in gradle.split(".")[:2])
    assert gradle_tuple >= required, (
        f"AGP {agp} requires Gradle >= {required[0]}.{required[1]}, "
        f"but the wrapper pins {gradle}"
    )


# --------------------------------------------------------------------------- #
# 2. toolchain coherence
# --------------------------------------------------------------------------- #
def test_agp_supports_the_configured_compile_sdk():
    agp = agp_version()
    line = ".".join(agp.split(".")[:2])
    compile_sdk = int(app_config()["compileSdk"])
    ceiling = AGP_MAX_COMPILE_SDK.get(line)
    assert ceiling is not None, f"unknown AGP line {line}; update AGP_MAX_COMPILE_SDK"
    assert compile_sdk <= ceiling, (
        f"compileSdk {compile_sdk} exceeds what AGP {agp} supports ({ceiling}); "
        "downgrade compileSdk or upgrade AGP"
    )


def test_compile_and_target_sdk_agree():
    cfg = app_config()
    assert cfg["compileSdk"] == cfg["targetSdk"], (
        "compileSdk and targetSdk should move together; a mismatch changes runtime "
        "behaviour in ways that are hard to reason about"
    )


def test_compose_compiler_matches_kotlin_version():
    kotlin = kotlin_version()
    configured = app_config()["kotlinCompilerExtensionVersion"]
    expected = KOTLIN_COMPOSE_COMPILER.get(kotlin)
    assert expected, f"Kotlin {kotlin} is not in KOTLIN_COMPOSE_COMPILER; add it"
    assert configured == expected, (
        f"Kotlin {kotlin} needs compose compiler {expected}, found {configured}"
    )


def test_compose_bom_is_compatible_with_compile_sdk():
    match = re.search(r"androidx\.compose:compose-bom:([\d.]+)", read(APP_BUILD))
    assert match, "Compose BOM is not pinned"
    bom = match.group(1)
    compile_sdk = int(app_config()["compileSdk"])
    if bom >= BOM_REQUIRES_SDK_35_FROM:
        assert compile_sdk >= 35, (
            f"Compose BOM {bom} requires compileSdk 35, but the project sets "
            f"{compile_sdk}. Pin an older BOM or bump compileSdk + AGP together."
        )


def test_java_and_kotlin_targets_are_17():
    text = read(APP_BUILD)
    assert "JavaVersion.VERSION_17" in text
    assert 'jvmTarget = "17"' in text


def test_min_sdk_matches_the_documented_floor():
    assert int(app_config()["minSdk"]) == 26, (
        "minSdk 26 is the documented floor (NotificationChannel, foreground "
        "service rules); changing it needs a docs update"
    )


# --------------------------------------------------------------------------- #
# 3. manifest correctness
# --------------------------------------------------------------------------- #
def test_connected_device_foreground_service_has_its_permissions():
    text = read(MANIFEST)
    assert 'android:foregroundServiceType="connectedDevice"' in text
    perms = permissions()
    assert "android.permission.FOREGROUND_SERVICE" in perms
    assert "android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE" in perms, (
        "Android 14+ throws SecurityException without this"
    )
    assert perms & FGS_CONNECTED_DEVICE_PREREQUISITES, (
        "connectedDevice also requires one of "
        f"{sorted(FGS_CONNECTED_DEVICE_PREREQUISITES)}; the app declares none"
    )


def test_every_declared_permission_is_a_real_permission_string():
    known_prefixes = ("android.permission.", "com.android.", "dev.phonebridge.")
    for perm in permissions():
        assert perm.startswith(known_prefixes), f"unexpected permission {perm}"
        assert not perm.endswith(" "), f"trailing space in {perm!r}"
        assert re.fullmatch(r"[A-Za-z0-9_.]+", perm), f"malformed permission {perm!r}"


def test_internet_permission_is_declared():
    assert "android.permission.INTERNET" in permissions()


def test_backup_is_disabled_and_components_are_scoped():
    text = read(MANIFEST)
    assert 'android:allowBackup="false"' in text, "identity keys must not be backed up"
    assert 'android:name=".MainActivity"' in text
    assert 'android:exported="true"' in text, "the launcher activity must be exported"
    assert 'android:name=".service.BridgeService"' in text
    assert re.search(
        r'<service[^>]*android:name="\.service\.BridgeService"[^>]*android:exported="false"',
        text,
        re.S,
    ), "the bridge service must not be exported to other apps"


def test_declared_permissions_are_exactly_the_ones_the_code_uses():
    """No silent permission creep.

    Phase 1 needed none of these; the capability probe needs exactly these. The
    test pins the set so adding a permission is a deliberate, reviewed change
    rather than an accident, and so a permission the UI *requests* can never be
    missing from the manifest (that fails silently on device).
    """
    expected = {
        "android.permission.INTERNET",
        "android.permission.ACCESS_NETWORK_STATE",
        "android.permission.ACCESS_WIFI_STATE",
        "android.permission.FOREGROUND_SERVICE",
        "android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE",
        "android.permission.WAKE_LOCK",
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.CHANGE_NETWORK_STATE",
        "android.permission.CHANGE_WIFI_STATE",
        "android.permission.MODIFY_AUDIO_SETTINGS",
        "android.permission.RECORD_AUDIO",
        "android.permission.BLUETOOTH_CONNECT",
        "android.permission.BLUETOOTH",
        "android.permission.CAMERA",
    }
    declared = permissions()
    assert declared == expected, (
        f"unexpected: {sorted(declared - expected)}; "
        f"missing: {sorted(expected - declared)}"
    )


def test_storage_and_scanning_permissions_are_not_requested():
    """CAMERA left this list in Phase 4, when a feature started using it.

    Every remaining entry is a permission the app has no feature for: holding one
    would be the same false capability claim the matrix is supposed to prevent.
    """
    forbidden = {
        "android.permission.MANAGE_EXTERNAL_STORAGE",
        "android.permission.READ_EXTERNAL_STORAGE",
        "android.permission.WRITE_EXTERNAL_STORAGE",
        "android.permission.READ_SMS",
        "android.permission.ACCESS_FINE_LOCATION",
    }
    declared = permissions() & forbidden
    assert not declared, f"these are not needed for any implemented feature: {sorted(declared)}"


def test_legacy_bluetooth_permission_is_capped_at_api_30():
    text = read(MANIFEST)
    match = re.search(
        r'<uses-permission\s+android:name="android.permission.BLUETOOTH"\s+'
        r'android:maxSdkVersion="(\d+)"',
        text,
    )
    assert match, (
        "the legacy BLUETOOTH permission must be limited with maxSdkVersion; "
        "unlimited it is a redundant install-time permission on API 31+"
    )
    assert match.group(1) == "30"


def test_every_runtime_permission_requested_in_code_is_declared():
    """A permission requested at runtime but absent from the manifest is denied
    instantly and silently - a classic Android bug that is invisible until it
    matters."""
    declared = permissions()
    requested: set[str] = set()
    for kt in KOTLIN_DIR.rglob("*.kt"):
        text = kt.read_text("utf-8")
        for name in re.findall(r"Manifest\.permission\.([A-Z_0-9]+)", text):
            requested.add(f"android.permission.{name}")
    missing = requested - declared
    assert not missing, (
        f"code requests {sorted(missing)} but the manifest does not declare them"
    )


# --------------------------------------------------------------------------- #
# 4. reproducibility of the dependency set
# --------------------------------------------------------------------------- #
def test_no_dynamic_dependency_versions():
    for name in (APP_BUILD, ROOT_BUILD):
        text = read(name)
        for bad in ("latest.release", "latest.integration", "1.+", "+)"):
            assert bad not in text, f"{name.name} contains a dynamic version ({bad})"


def test_every_dependency_coordinate_is_well_formed():
    pattern = re.compile(
        r'(?:implementation|debugImplementation|testImplementation|androidTestImplementation|'
        r'api|kapt)\(\s*(?:platform\()?"([^"]+)"'
    )
    text = read(APP_BUILD)
    coords = pattern.findall(text)
    assert coords, "no dependencies parsed from app/build.gradle.kts"
    has_bom = "platform(" in text
    for coord in coords:
        if re.fullmatch(r"[\w.\-]+:[\w.\-]+:[\w.\-]+", coord):
            continue
        # group:artifact without a version is only valid when a BOM supplies it.
        assert re.fullmatch(r"[\w.\-]+:[\w.\-]+", coord) and has_bom, (
            f"dependency {coord!r} has no version and no BOM provides one"
        )


def test_proguard_file_referenced_by_the_release_build_exists():
    text = read(APP_BUILD)
    if "proguard-rules.pro" not in text:
        pytest.skip("no custom proguard file referenced")
    assert (ANDROID / "app" / "proguard-rules.pro").is_file()


def test_local_properties_is_gitignored():
    assert "android/local.properties" in read(REPO / ".gitignore"), (
        "local.properties contains an absolute SDK path and must never be committed"
    )


def test_gradle_properties_enables_androidx_and_sane_heaps():
    text = read(PROPS)
    assert "android.useAndroidX=true" in text
    assert "org.gradle.jvmargs" in text


def test_sdk_location_is_documented_somewhere():
    docs = " ".join(
        p.read_text("utf-8")
        for p in (REPO / "docs").glob("*.md")
    )
    assert "sdk.dir" in docs, (
        "the CLI build path needs local.properties with sdk.dir; document it"
    )


# --------------------------------------------------------------------------- #
# 5. locale-dependent formatting (a real device-only failure mode)
# --------------------------------------------------------------------------- #
def test_sas_is_formatted_with_an_explicit_locale():
    """`"%06d".format(x)` uses the default locale, which renders Arabic-Indic
    digits on such devices. The phone would then show a different SAS than the
    PC and the user would think the link was intercepted."""
    text = read(ANDROID / "app/src/main/java/dev/phonebridge/protocol/Crypto.kt")
    block = text.split("fun sasDigits(", 1)[1].split("\n    }", 1)[0]
    assert "Locale.ROOT" in block, "sasDigits must pass Locale.ROOT to format()"


def test_hex_formatting_uses_locale_root():
    crypto = read(ANDROID / "app/src/main/java/dev/phonebridge/protocol/Crypto.kt")
    assert '"%02x".format(Locale.ROOT' in crypto
