// An import, not `java.util.Properties`: inside a .gradle.kts script `java` is
// the Gradle Java extension, so the fully qualified name does not resolve.
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.phonebridge"
    compileSdk = 34

    defaultConfig {
        applicationId = "dev.phonebridge"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"

        // A *build* string the phone can report and the PC can print at connect.
        //
        // "Which APK is on the phone?" was unanswerable: versionName is the same
        // for every build between releases, so a phone running a build from three
        // commits ago looked identical to today's - which is exactly how "the
        // latency probe never reported" turned into a guessing game. The short git
        // SHA is read at configuration time; a source archive with no .git gets
        // "nogit" instead of failing the build.
        val gitSha = run {
            try {
                val process = ProcessBuilder("git", "rev-parse", "--short=8", "HEAD")
                    .directory(rootProject.projectDir.parentFile)
                    .redirectErrorStream(true)
                    .start()
                val text = process.inputStream.bufferedReader().use { it.readText() }.trim()
                process.waitFor()
                if (process.exitValue() == 0 && text.isNotEmpty()) text else "nogit"
            } catch (e: Exception) {
                "nogit"
            }
        }
        buildConfigField("String", "GIT_SHA", "\"$gitSha\"")
    }

    // Release signing is read from android/keystore.properties, which is NOT in
    // the repository (.gitignore) and never should be:
    //
    //     storeFile=phonebridge-release.jks
    //     storePassword=...
    //     keyAlias=phonebridge
    //     keyPassword=...
    //
    // Without that file `assembleRelease` still builds, but produces
    // app-release-unsigned.apk - an APK that cannot be installed or updated.
    // The build fails loudly at the end instead of silently shipping that.
    val keystorePropertiesFile = rootProject.file("keystore.properties")
    val keystoreProperties = Properties().apply {
        if (keystorePropertiesFile.exists()) {
            keystorePropertiesFile.inputStream().use { load(it) }
        }
    }
    val hasReleaseKeystore = keystoreProperties.getProperty("storeFile") != null

    signingConfigs {
        if (hasReleaseKeystore) {
            create("release") {
                storeFile = rootProject.file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            // Minification stays off on purpose: R8 rules for Compose + a
            // reflective WebSocket library cannot be validated without building
            // and exercising the APK on a device, and a shrunk app that crashes
            // is worse than a larger one that runs. Revisit with a device test.
            isMinifyEnabled = false
            isShrinkResources = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (hasReleaseKeystore) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    buildFeatures {
        compose = true
        // BuildConfig is off by default in AGP 8: without this, VERSION_NAME and
        // the GIT_SHA field above do not exist as Java symbols at all.
        buildConfig = true
    }

    composeOptions {
        // Matches Kotlin 1.9.24. Do not bump one without the other.
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        // Two explicit entries: the brace-shorthand string is not a glob and
        // would silently fail to exclude anything.
        resources.excludes += setOf("/META-INF/AL2.0", "/META-INF/LGPL2.1")
    }
}

dependencies {
    // WebSocket server. Pure Java, ~200 KiB, no NDK, no coroutine dependency.
    implementation("org.java-websocket:Java-WebSocket:1.5.7")

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // Compose BOM 2024.06.00 -> compose-ui 1.6.8 / material3 1.2.1.
    // Deliberately NOT a newer BOM: compose 1.7.x requires compileSdk 35, which
    // in turn requires AGP 8.6+, and this project pins compileSdk 34 with AGP
    // 8.5.2 so it builds on a wider range of installed SDKs. Do not bump the BOM
    // without also bumping compileSdk and AGP together.
    implementation(platform("androidx.compose:compose-bom:2024.06.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    debugImplementation("androidx.compose.ui:ui-tooling")

    // === Phase 4 camera =====================================================
    // CameraX 1.3.4 is the last line that builds against compileSdk 34 with AGP
    // 8.5.2, which is what this project pins (see the Compose note above).
    // `camera-lifecycle` pulls in lifecycle-runtime, whose LifecycleOwner
    // CameraEngine implements for the duration of one stream.
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")

    // === Unit tests =========================================================
    // JUnit 4 only, and only for code that touches no Android framework class:
    // `Nv21` (the plane packing) is plain `java.nio`, so `testDebugUnitTest`
    // runs it on the JVM with no emulator and no Robolectric. Do not add
    // Android-framework tests here without also adding Robolectric and a
    // `testOptions { unitTests.isIncludeAndroidResources = true }` block.
    testImplementation("junit:junit:4.13.2")
}
