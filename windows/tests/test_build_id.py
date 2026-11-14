"""Which APK is on the phone? A build id, printed where the question is asked.

The user's report: ``speaker --latency-test`` said *"the phone never reported the
probe reaching its mixer"* — and the honest answer at the time was "unknown",
because nothing on the wire carried a version. An APK older than the probe and an
APK with a broken probe produce the same silence, and one of those is fixed by
reinstalling.

So the phone now names itself three times: in the handshake (``build`` plus the
feature flags), in ``system.info`` (``build_id``, ``git_sha``), and in
``audio.status``/``camera.status`` (``build_id``). The PC prints it at connect and
next to any "the phone never reported X" sentence. A missing build id is not
noise — it *is* the diagnosis: that APK predates the field.
"""

from __future__ import annotations

import re
from pathlib import Path

from phonebridge.core.session import PhoneInfo

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "android" / "app"
JAVA = APP / "src" / "main" / "java" / "dev" / "phonebridge"


def kotlin(relative: str) -> str:
    return (JAVA / relative).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the phone side
# --------------------------------------------------------------------------- #
def test_the_build_id_is_version_versioncode_and_commit() -> None:
    """``versionName`` alone cannot tell two builds of the same release apart."""
    source = kotlin("BuildInfo.kt")

    assert "BuildConfig.VERSION_NAME" in source
    assert "BuildConfig.VERSION_CODE" in source
    assert "BuildConfig.GIT_SHA" in source
    assert "val id" in source
    # The commit, when gradle could read one, and a word that means "unknown"
    # when it could not — never a crash and never a stale constant.
    assert '"nogit"' in (APP / "build.gradle.kts").read_text(encoding="utf-8")


def test_the_features_are_advertised_as_flags() -> None:
    """A missing feature must read ``false`` in the handshake, not time out."""
    source = kotlin("BuildInfo.kt")

    for feature in ("latency_probe", "fps_range", "adaptive_queue"):
        assert feature in source, feature
    assert "mapOf(" in source and " to true" in source


def test_gradle_emits_the_commit_as_a_build_config_field() -> None:
    gradle = (APP / "build.gradle.kts").read_text(encoding="utf-8")

    assert 'buildConfigField("String", "GIT_SHA"' in gradle
    assert "rev-parse" in gradle, "the commit comes from git, not from a constant"
    # AGP 8 needs this or BuildConfig has no fields at all.
    assert re.search(r"buildFeatures\s*\{[^}]*buildConfig\s*=\s*true", gradle, re.S)


def test_every_status_and_the_handshake_carry_the_build() -> None:
    audio = kotlin("net/AudioEngine.kt")
    camera = kotlin("net/CameraEngine.kt")
    facts = kotlin("net/DeviceFacts.kt")
    server = kotlin("net/BridgeServer.kt")

    assert '"build_id" to BuildInfo.id' in audio, "audio.status"
    assert '"build_id" to BuildInfo.id' in camera, "camera.status"
    assert '"build_id" to BuildInfo.id' in facts, "system.info"
    assert '"git_sha" to BuildInfo.gitSha' in facts
    # auth_ok and pair_required: both are handshakes the PC can see first.
    assert server.count('"build" to BuildInfo.id') == 2
    assert server.count('"features" to BuildInfo.features') == 2


def test_the_kotlin_brings_its_imports() -> None:
    """``BuildInfo`` is in ``dev.phonebridge``; without the import it will not compile."""
    for relative in ("net/AudioEngine.kt", "net/CameraEngine.kt", "net/DeviceFacts.kt",
                     "net/BridgeServer.kt"):
        source = kotlin(relative)
        uses = "BuildInfo." in source
        assert uses, relative
        assert "import dev.phonebridge.BuildInfo" in source, relative


# --------------------------------------------------------------------------- #
# the PC side
# --------------------------------------------------------------------------- #
def test_the_handshake_reply_is_parsed_into_phoneinfo() -> None:
    source = (REPO / "windows" / "phonebridge" / "core" / "session.py").read_text(encoding="utf-8")

    # Both replies: the paired path (auth_ok) and the pairing path.
    assert source.count('build=str(reply.get("build") or "")') == 2
    assert source.count('features=dict(reply.get("features") or {})') == 2


def test_an_apk_older_than_the_field_reports_nothing() -> None:
    phone = PhoneInfo(device_id="d", name="phone", paired=True, sas_digits="")

    assert phone.build == ""
    assert phone.features == {}
    assert phone.has("latency_probe") is False, "absent must not raise, and must not lie"


def test_a_current_apk_answers_what_it_supports() -> None:
    phone = PhoneInfo(
        device_id="d",
        name="phone",
        paired=True,
        sas_digits="",
        build="1.0.0 (1) e807c93a",
        features={"latency_probe": True, "fps_range": True},
    )

    assert phone.has("latency_probe") is True
    assert phone.has("fps_range") is True
    assert phone.has("adaptive_queue") is False, "a feature nobody advertised is absent"


def test_the_connect_line_prints_the_build() -> None:
    source = (REPO / "windows" / "phonebridge" / "cli" / "target.py").read_text(encoding="utf-8")

    assert "session.phone.build" in source
    assert "app build not reported" in source, "an absent build is a finding, so say it"


def test_the_latency_test_checks_the_feature_before_it_measures() -> None:
    """Six seconds of waiting to learn something the handshake already said is rude."""
    source = (REPO / "windows" / "phonebridge" / "cli" / "live.py").read_text(encoding="utf-8")

    body = source[source.index("async def _run_latency_test"):]
    assert 'phone.has("latency_probe")' in body
    assert "predates the latency probe" in body
    before = body.index('phone.has("latency_probe")')
    after = body.index("measuring the speaker path")
    assert before < after, "the build is checked before the measurement starts"


def test_the_device_block_shows_the_build_and_the_commit() -> None:
    source = (REPO / "windows" / "phonebridge" / "cli" / "format.py").read_text(encoding="utf-8")

    assert '"build_id": info.get("build_id")' in source
    assert '"git_sha": info.get("git_sha")' in source
    assert '("build_id", "build")' in source


def test_the_selftest_does_not_require_the_new_keys() -> None:
    """An old APK must still pass the selftest — it just says what it cannot report."""
    source = (REPO / "windows" / "phonebridge" / "cli" / "selftest.py").read_text(encoding="utf-8")

    required = source[source.index("INFO_KEYS = {"):source.index("}", source.index("INFO_KEYS = {"))]
    assert "build_id" not in required, "requiring it would fail every older APK"
    assert "OPTIONAL_INFO_KEYS" in source
    assert "app build not reported" in source
