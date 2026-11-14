# PhoneBridge virtual camera — proof of concept (build file).
#
# Two outputs, deliberately separate:
#   PhoneBridgeVcamSource.dll  the COM media source the frame server loads
#   vcam_host.exe              registers it, starts the camera, removes it on exit
#
# The DLL is the part that would ship; the host is scaffolding that the real
# implementation replaces with PhoneBridge's own service.
#
#   cmake -S windows-native/vcam-poc -B build/vcam -G "Visual Studio 17 2022" -A x64
#   cmake --build build/vcam --config Release
#
# NOT COMPILED HERE: no MSVC and no Windows SDK in this environment.

cmake_minimum_required(VERSION 3.20)
project(PhoneBridgeVcamPoc CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT WIN32)
    message(FATAL_ERROR "The virtual camera is Windows-only (Media Foundation).")
endif()
if(CMAKE_SIZEOF_VOID_P EQUAL 4)
    # The virtual camera API is x64/arm64 only; there is no 32-bit build.
    message(FATAL_ERROR "Build for x64 (or arm64); x86 is not supported.")
endif()

# Fail at configure time with a readable message instead of a wall of missing
# headers later: mfvirtualcamera.h ships with the Windows 11 SDK.
# A warning rather than an error: `find_path` can miss the SDK's include
# directory on a machine where the compiler still finds it, and a false negative
# would block a build that would have worked.
find_path(MFVIRTUALCAMERA_HEADER mfvirtualcamera.h)
if(MFVIRTUALCAMERA_HEADER)
    message(STATUS "mfvirtualcamera.h: ${MFVIRTUALCAMERA_HEADER}")
else()
    message(WARNING
        "mfvirtualcamera.h not found on the default search path. If the build then fails "
        "on that include, install the Windows 11 SDK 10.0.22000 or later (Visual Studio "
        "Installer > Individual components > Windows 11 SDK).")
endif()

add_library(PhoneBridgeVcamSource SHARED
    src/media_source.cpp
    src/media_source_activate.cpp
    src/vcam.def
)
target_include_directories(PhoneBridgeVcamSource PRIVATE src)
target_compile_definitions(PhoneBridgeVcamSource PRIVATE
    UNICODE _UNICODE
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    WINVER=0x0A00 _WIN32_WINNT=0x0A00   # Windows 10+ SDK surface
)
target_link_libraries(PhoneBridgeVcamSource PRIVATE
    mfplat.lib
    mfuuid.lib
    mfsensorgroup.lib   # MFCreateVirtualCamera lives here
    ole32.lib
)
set_target_properties(PhoneBridgeVcamSource PROPERTIES
    OUTPUT_NAME "PhoneBridgeVcamSource"
    PREFIX ""            # no "lib" prefix on Windows
)
# The .def in the source list above is what exports DllGetClassObject /
# DllCanUnloadNow under their undecorated names; no /DEF flag needed.

add_executable(vcam_host src/vcam_host.cpp)
target_include_directories(vcam_host PRIVATE src)
target_compile_definitions(vcam_host PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX
                                           WINVER=0x0A00 _WIN32_WINNT=0x0A00)
target_link_libraries(vcam_host PRIVATE
    mfplat.lib
    mfuuid.lib
    mfsensorgroup.lib
    ole32.lib
    advapi32.lib       # registry
)
# The host expects the DLL beside it, which is also what makes the per-user
# InprocServer32 path stable.
add_dependencies(vcam_host PhoneBridgeVcamSource)
set_target_properties(vcam_host PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "$<TARGET_FILE_DIR:PhoneBridgeVcamSource>"
)

if(MSVC)
    # /W4 with only the checks that matter for COM code; /permissive- catches the
    # COM interface mismatches that would otherwise fail at runtime.
    target_compile_options(PhoneBridgeVcamSource PRIVATE /W4 /permissive- /EHsc)
    target_compile_options(vcam_host PRIVATE /W4 /permissive- /EHsc)
endif()
