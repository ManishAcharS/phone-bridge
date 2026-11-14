package dev.phonebridge

/**
 * What build this APK is, in one string the PC can print at connect.
 *
 * "Which APK is on the phone?" used to be unanswerable. `versionName` is `1.0.0`
 * for every build between releases, so a phone running a build from three commits
 * ago looked identical to today's — and "the latency probe never reported" turned
 * into a guessing game about whether the phone even had that code.
 *
 * So the build now names itself:
 *
 * * [id] is `1.0.0 (1) e807c93a` — version, version code, short git SHA;
 * * [features] lists the things a PC may ask for, so a missing feature is a
 *   `false` in the handshake instead of a timeout.
 *
 * Both travel in the handshake (`auth_ok` / `pair_required`) and in every
 * `audio.status` / `camera.status` reply, so the answer survives a reconnect and
 * does not depend on which command asked first.
 *
 * The values come from `BuildConfig.GIT_SHA`, which Gradle fills in at build time;
 * a source archive with no `.git` gets `nogit` and still builds.
 */
object BuildInfo {

    /** `versionName` from `build.gradle.kts`, e.g. `1.0.0`. */
    val appVersion: String = BuildConfig.VERSION_NAME

    /** `versionCode`, e.g. `1`. */
    val versionCode: Int = BuildConfig.VERSION_CODE

    /** Short git SHA of the commit this APK was built from, or `nogit`. */
    val gitSha: String = BuildConfig.GIT_SHA

    /** `1.0.0 (1) e807c93a`: the whole answer, in one string. */
    val id: String = "$appVersion ($versionCode) $gitSha"

    /**
     * Capabilities that belong to the *build*, not to the hardware.
     *
     * The PC checks these before promising the user something: a phone whose APK
     * predates the latency probe says so here, and `speaker --latency-test` can
     * explain itself instead of waiting six seconds for an answer that will never
     * come.
     */
    val features: Map<String, Boolean> = mapOf(
        // audio.playback.start takes target_ms / adaptive / low_latency / probe and
        // audio.status reports queue_target_ms, probe_latency_ms and the A2DP note.
        "latency_probe" to true,
        // camera.start takes fixed_fps and camera.status reports fps_range and
        // fps_ranges_supported, read from CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES.
        "fps_range" to true,
        // The playback queue target adapts between 1 and 8 frames on underruns.
        "adaptive_queue" to true,
    )
}
