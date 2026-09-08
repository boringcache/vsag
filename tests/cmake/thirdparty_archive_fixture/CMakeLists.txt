cmake_minimum_required (VERSION 3.18)
project (thirdparty_archive NONE)
include (ExternalProject)
include (${VSAG_SOURCE_DIR}/cmake/VSAGThirdPartyOverride.cmake)
set (urls https://example.invalid/archive.txt)
vsag_resolve_thirdparty_override (FMT 10.2.1 urls)
ExternalProject_Add (archive
    URL ${urls}
    URL_HASH SHA256=${TEST_ARCHIVE_HASH}
    DOWNLOAD_NO_EXTRACT TRUE
    CONFIGURE_COMMAND ""
    BUILD_COMMAND ""
    INSTALL_COMMAND "")
