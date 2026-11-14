"""The camera must be driven from the main thread, and must say so when it fails.

The phone's own logcat gave the root cause:

    E BridgeServer: RPC camera.start failed: Method setCurrentState must be
    called on the main thread

`camera.start` runs on a Java-WebSocket thread. CameraX drives a
`LifecycleRegistry` internally (`bindToLifecycle`, `unbindAll`) and
`LifecycleRegistry.setCurrentState` is main-thread-only, so the bind threw, the
phone answered `internal`, and the PC showed "camera error" with nothing else.

There is no Android SDK or emulator in this environment, so — exactly like
`test_phase6_android_camera.py` — these tests read the Kotlin and pin the
structure that makes the failure impossible: every lifecycle call happens inside
one main-thread helper, that helper is waited on, and every failure is logged
with its own tag.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE = (
    REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
    / "net" / "CameraEngine.kt"
)


def source() -> str:
    return ENGINE.read_text(encoding="utf-8")


def body(function: str) -> str:
    """The text of one Kotlin function, from its signature to the next one."""
    text = source()
    start = text.index(function)
    rest = text[start + len(function):]
    # A function ends at the next top-level `    fun `/`    private fun ` line.
    ends = [
        rest.index(marker)
        for marker in ("\n    fun ", "\n    private fun ", "\n    internal fun ")
        if marker in rest
    ]
    return rest[: min(ends)] if ends else rest


# --------------------------------------------------------------------------- #
# the main thread
# --------------------------------------------------------------------------- #
def test_the_lifecycle_work_runs_through_one_main_thread_helper() -> None:
    text = source()

    assert 'private val mainHandler = Handler(Looper.getMainLooper())' in text
    helper = body("private fun <T> onMainThread(")
    assert "CountDownLatch" in helper, "the caller must wait for the main thread"
    assert "mainHandler.post" in helper
    assert "await(timeoutMs" in helper, "an unbounded wait is a hung RPC"
    assert 'Looper.getMainLooper()' in helper and "myLooper()" in helper, (
        "calling from the main thread must run inline, not post and wait on itself"
    )


def test_start_binds_on_the_main_thread_and_waits() -> None:
    start = body("fun start(requested: VideoFrames.Config)")
    assert 'onMainThread("camera.start")' in start
    assert "bindOnMainThread(" in start
    # The provider future completes on the main thread: fetching it there would
    # deadlock, so the fetch stays outside the hop.
    assert "obtainProvider()" in start
    assert "future.get(" not in start


def test_every_lifecycle_call_lives_in_the_main_thread_block() -> None:
    bind = body("private fun bindOnMainThread(")
    assert "CameraLifecycle()" in bind, "the owner's RESUMED transition is a main-thread call"
    assert "bindToLifecycle(" in bind
    assert "unbindAll()" in bind

    text = source()
    assert text.count("CameraLifecycle()") == 1, "no second, off-thread owner"
    assert text.count("bindToLifecycle(") == 1
    # Three places unbind, and each is inside main-thread work: the bind block,
    # its own failure cleanup, and the stop path reached through onMainThread.
    stop_locked = body("private fun stopLocked(")
    assert "unbindAll()" in stop_locked
    assert text.count("unbindAll()") == 3


def test_stop_also_waits_for_the_main_thread() -> None:
    stop = body("fun stop()")
    assert "closeCamera(" in stop

    close = body("private fun closeCamera(")
    assert 'onMainThread("camera.stop")' in close
    assert "stopLocked(reason)" in close
    assert "catch (e: CameraFailure)" in close, "a stop must never throw into session teardown"
    assert stop.count("stopLocked(") == 0, "stop() itself must not touch the lifecycle off-thread"


def test_a_blocked_main_thread_becomes_a_sentence_not_a_hang() -> None:
    helper = body("private fun <T> onMainThread(")
    assert "CameraFailure(" in helper
    assert "main thread is busy" in helper
    assert "the phone's main thread is not accepting work" in helper


def test_the_provider_is_never_blocked_on_from_the_main_thread() -> None:
    provider = body("private fun obtainProvider(")
    assert "onMain && !future.isDone" in provider
    assert "still starting up" in provider


# --------------------------------------------------------------------------- #
# the failure has to be visible
# --------------------------------------------------------------------------- #
def test_failures_are_logged_with_the_cameras_own_tag() -> None:
    text = source()

    assert 'private const val TAG = "CameraEngine"' in text, (
        "adb logcat -s CameraEngine must be a real command"
    )
    logger = body("private fun logFailure(")
    assert "Log.e(TAG, line" in logger

    for where in (
        "private fun bindOnMainThread(",
        "private fun obtainProvider(",
        "private fun <T> onMainThread(",
        "private fun closeCamera(",
    ):
        assert "logFailure(" in body(where), f"{where} must log why it gave up"


def test_every_refusal_carries_a_documented_code() -> None:
    text = source()
    for code in ("no_camera", "camera_denied", "camera_busy", "internal"):
        assert f'"{code}"' in text, f"{code} is part of the PC-visible contract"

    start = body("fun start(requested: VideoFrames.Config)")
    assert "fail(" in start
    assert '"no_camera"' in start
    assert '"camera_denied"' in start

    bind = body("private fun bindOnMainThread(")
    assert '"camera_busy"' in bind


def test_switch_is_a_start_or_a_stop_and_never_a_third_path() -> None:
    """`camera.switch` in BridgeServer must reuse the safe start/stop."""
    server = (
        REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
        / "net" / "BridgeServer.kt"
    ).read_text(encoding="utf-8")
    switch = server[server.index('"camera.switch" ->'):]
    switch = switch[: switch.index('"camera.status" ->')]

    assert "camera.start(next)" in switch
    assert "camera.stop()" in switch
    assert "bindToLifecycle" not in switch, "switch must not grow its own binding path"


def test_camera_rpcs_answer_off_the_peer_session_thread() -> None:
    """The handler runs holding the peer lock; waiting for the main thread there
    would make the main thread wait for that lock in its own stop path."""
    server = (
        REPO / "android" / "app" / "src" / "main" / "java" / "dev" / "phonebridge"
        / "net" / "BridgeServer.kt"
    ).read_text(encoding="utf-8")

    camera_block = server[server.index('"camera.start" ->'): server.index('"camera.status" ->')]
    assert camera_block.count("answerLater(peer, id,") == 3, (
        "start, stop and switch all answer from the camera thread"
    )
    assert camera_block.count("cameraExecutor") == 3

    assert 'Thread(runnable, "phonebridge-camera-rpc")' in server
    assert "cameraExecutor.shutdownNow()" in server, "the thread must not outlive the server"
    assert server.index("camera.release()") < server.index("cameraExecutor.shutdownNow()"), (
        "a queued camera.stop must be able to release the lens first"
    )
