include_guard (GLOBAL)

function (vsag_resolve_prebuilt dependency pin source_hash cxx_flags install_output hit_output)
    set (${hit_output} FALSE PARENT_SCOPE)
    if (NOT VSAG_USE_PREBUILT_DEPS)
        message (STATUS "Dependency ${dependency}: source (prebuilt installations disabled)")
        return ()
    endif ()
    if (NOT CMAKE_SYSTEM_NAME STREQUAL "Linux"
        OR NOT VSAG_TARGET_PROCESSOR STREQUAL "x86_64"
        OR NOT CMAKE_CXX_COMPILER_ID STREQUAL "GNU"
        OR ENABLE_LIBCXX OR CMAKE_CROSSCOMPILING)
        message (STATUS "Dependency ${dependency}: source (outside the Linux x86_64 GCC pilot)")
        return ()
    endif ()

    find_package (Python3 3.8 REQUIRED COMPONENTS Interpreter)
    set (script "${PROJECT_SOURCE_DIR}/scripts/ci/dependency_cache.py")
    set (metadata "${CMAKE_BINARY_DIR}/.vsag-dependency-cache")
    set (prefix "${VSAG_PREBUILT_DEPS_DIR}/${dependency}")
    set (spec "${metadata}/${dependency}.json")
    set (fields)
    foreach (variable IN ITEMS
            CMAKE_SYSTEM_NAME CMAKE_SYSTEM_PROCESSOR CMAKE_LIBRARY_ARCHITECTURE
            CMAKE_C_COMPILER_ID CMAKE_C_COMPILER_VERSION CMAKE_C_COMPILER_TARGET
            CMAKE_CXX_COMPILER_ID CMAKE_CXX_COMPILER_VERSION CMAKE_CXX_COMPILER_TARGET
            CMAKE_SYSROOT CMAKE_BUILD_TYPE CMAKE_CXX_STANDARD CMAKE_C_FLAGS CMAKE_CXX_FLAGS
            CMAKE_EXE_LINKER_FLAGS CMAKE_SHARED_LINKER_FLAGS CMAKE_VERSION
            ENABLE_CXX11_ABI ENABLE_LIBCXX ENABLE_ASAN ENABLE_TSAN ENABLE_COVERAGE
            ENABLE_THIN_LTO VSAG_THIRDPARTY_C_FLAGS VSAG_THIRDPARTY_EXE_LINKER_FLAGS
            VSAG_THIRDPARTY_SHARED_LINKER_FLAGS CMAKE_INSTALL_PREFIX)
        list (APPEND fields --field "${variable}=${${variable}}")
    endforeach ()
    execute_process (
        COMMAND "${Python3_EXECUTABLE}" -m scripts.ci.dependency_cache plan
            --dependency "${dependency}" --pin "${pin}" --source-hash "${source_hash}"
            --cc "${CMAKE_C_COMPILER}" --cxx "${CMAKE_CXX_COMPILER}"
            --recipe "${PROJECT_SOURCE_DIR}/extern/${dependency}/${dependency}.cmake"
            --recipe "${PROJECT_SOURCE_DIR}/cmake/VSAGExternalProjectConfig.cmake"
            --recipe "${PROJECT_SOURCE_DIR}/cmake/VSAGPrebuiltDependencies.cmake"
            --recipe "${script}" --field "cxx_flags=${cxx_flags}"
            ${fields} --output "${spec}"
        RESULT_VARIABLE result OUTPUT_VARIABLE digest ERROR_VARIABLE detail
        WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
        OUTPUT_STRIP_TRAILING_WHITESPACE)
    if (NOT result EQUAL 0)
        message (FATAL_ERROR "Cannot fingerprint ${dependency}: ${detail}")
    endif ()
    file (WRITE "${metadata}/${dependency}.key" "${digest}\n")
    execute_process (
        COMMAND "${Python3_EXECUTABLE}" -m scripts.ci.dependency_cache inspect --spec "${spec}"
            --prefix "${prefix}" --report "${metadata}/${dependency}-selection.json"
        RESULT_VARIABLE result OUTPUT_VARIABLE state ERROR_VARIABLE detail
        WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}"
        OUTPUT_STRIP_TRAILING_WHITESPACE)
    if (result EQUAL 0 AND state STREQUAL "hit")
        set (${install_output} "${prefix}" PARENT_SCOPE)
        set (${hit_output} TRUE PARENT_SCOPE)
        message (STATUS "Dependency ${dependency}: prebuilt (validated ${digest})")
    else ()
        message (STATUS "Dependency ${dependency}: source (no valid installation for ${digest})")
    endif ()
endfunction ()
