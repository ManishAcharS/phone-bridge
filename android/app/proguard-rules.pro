# PhoneBridge ProGuard/RGA rules.
#
# Release builds currently set isMinifyEnabled = false, so this file exists to
# keep the release variant's configuration resolvable. When minification is
# enabled these rules keep the reflective bits alive:
#   - Java-WebSocket resolves its frame classes dynamically
#   - org.json is part of the platform, nothing to keep
-keep class org.java_websocket.** { *; }
-keepclassmembers class dev.phonebridge.protocol.** { *; }
-dontwarn org.slf4j.**
